"""M1.2 V2 Incident and InvestigationRound lifecycle contracts."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from devsupport_backend.agent.state import (
    EvidenceContext,
    HypothesisContext,
    HypothesisStatus,
    ToolHistoryEntry,
    create_initial_agent_state,
)
from devsupport_backend.agent.v2_terminalization import V2Terminalizer
from devsupport_backend.investigation_lifecycle import (
    InvestigationLifecycleError,
    InvestigationLifecycleService,
    InvestigationRoundCreationError,
    InvestigationRoundService,
)
from devsupport_backend.investigation_status import (
    TERMINAL_INVESTIGATION_STATUSES,
    InvestigationStatus,
    legacy_status_projection,
)
from devsupport_backend.models import Evidence, Hypothesis, Incident, Observation, Report, ToolCall
from devsupport_backend.tools.registry import ToolName
from devsupport_backend.tools.schemas import ToolStatus


def _incident(session: Session, *, legacy_status: str = "OPEN") -> Incident:
    incident = Incident(
        service="order-service",
        environment="test",
        status=legacy_status,
        description="V2 lifecycle test incident.",
        time_range_start=datetime(2026, 9, 19, 8, tzinfo=UTC),
        time_range_end=datetime(2026, 9, 19, 9, tzinfo=UTC),
        thread_id=str(uuid4()),
    )
    session.add(incident)
    session.commit()
    return incident


def test_v2_status_contract_contains_only_the_five_product_statuses() -> None:
    assert {status.value for status in InvestigationStatus} == {
        "OPEN",
        "INVESTIGATING",
        "CONCLUDED",
        "INCONCLUSIVE",
        "FAILED",
    }
    assert {"WAITING_APPROVAL", "REMEDIATING", "RESOLVED", "NEEDS_MANUAL_ACTION"}.isdisjoint(
        {status.value for status in InvestigationStatus}
    )


@pytest.mark.parametrize(
    "terminal_status",
    sorted(TERMINAL_INVESTIGATION_STATUSES, key=lambda status: status.value),
)
def test_open_round_starts_and_terminalizes_with_its_incident(
    database_session: Session, terminal_status: InvestigationStatus
) -> None:
    incident = _incident(database_session)
    round_record = incident.rounds[0]
    lifecycle = InvestigationLifecycleService(database_session)

    started = lifecycle.start(round_record.id)
    assert started.status is InvestigationStatus.INVESTIGATING
    database_session.refresh(incident)
    assert incident.investigation_status is InvestigationStatus.INVESTIGATING

    terminal = lifecycle.terminalize(
        round_record.id,
        terminal_status,
        terminal_reason="bounded lifecycle test",
    )
    database_session.refresh(incident)
    assert terminal.status is terminal_status
    assert terminal.completed_at is not None
    assert incident.investigation_status is terminal_status

    with pytest.raises(InvestigationLifecycleError):
        lifecycle.start(round_record.id)


def test_lifecycle_rejects_an_illegal_incident_and_round_status_pair(
    database_session: Session,
) -> None:
    incident = _incident(database_session)
    round_record = incident.rounds[0]
    incident.investigation_status = InvestigationStatus.INVESTIGATING
    database_session.commit()

    with pytest.raises(InvestigationLifecycleError):
        InvestigationLifecycleService(database_session).start(round_record.id)


@pytest.mark.parametrize(
    ("legacy_status", "expected"),
    [
        ("WAITING_APPROVAL", InvestigationStatus.INVESTIGATING),
        ("REMEDIATING", InvestigationStatus.INVESTIGATING),
        ("RESOLVED", InvestigationStatus.CONCLUDED),
        ("NEEDS_MANUAL_ACTION", InvestigationStatus.INCONCLUSIVE),
    ],
)
def test_legacy_statuses_are_projected_without_joining_the_v2_contract(
    database_session: Session, legacy_status: str, expected: InvestigationStatus
) -> None:
    incident = _incident(database_session, legacy_status=legacy_status)

    assert incident.status == legacy_status
    assert incident.investigation_status is expected
    assert incident.rounds[0].status is expected
    assert legacy_status_projection(legacy_status) is expected


def test_database_rejects_values_outside_the_v2_status_contract(database_session: Session) -> None:
    incident = _incident(database_session)
    round_record = incident.rounds[0]

    with pytest.raises(IntegrityError):
        database_session.execute(
            text("UPDATE incidents SET investigation_status = 'RESOLVED' WHERE id = :id"),
            {"id": incident.id},
        )
    database_session.rollback()

    with pytest.raises(IntegrityError):
        database_session.execute(
            text("UPDATE investigation_rounds SET status = 'WAITING_APPROVAL' WHERE id = :id"),
            {"id": round_record.id},
        )
    database_session.rollback()


@pytest.mark.parametrize(
    "terminal_status",
    sorted(TERMINAL_INVESTIGATION_STATUSES, key=lambda status: status.value),
)
def test_terminal_round_cannot_restart_after_a_new_round(
    database_session: Session, terminal_status: InvestigationStatus
) -> None:
    incident = _incident(database_session)
    first_round = incident.rounds[0]
    lifecycle = InvestigationLifecycleService(database_session)
    lifecycle.start(first_round.id)
    lifecycle.terminalize(first_round.id, terminal_status)

    second_round = InvestigationRoundService(database_session).create_next_round(
        incident.id,
        Observation(
            content="终态后补充的观察。",
            observed_at=datetime(2026, 9, 19, 10, tzinfo=UTC),
        ),
    )

    assert second_round.round_number == 2
    assert second_round.thread_id != first_round.thread_id
    with pytest.raises(InvestigationLifecycleError):
        lifecycle.start(first_round.id)


def test_next_round_requires_a_terminal_incident(database_session: Session) -> None:
    incident = _incident(database_session)

    with pytest.raises(InvestigationRoundCreationError):
        InvestigationRoundService(database_session).create_next_round(
            incident.id,
            Observation(
                content="尚未终态时不能开始下一轮。",
                observed_at=datetime(2026, 9, 19, 10, tzinfo=UTC),
            ),
        )


def test_next_round_preserves_terminal_history_and_binds_its_observation(
    database_session: Session,
) -> None:
    incident = _incident(database_session)
    first_round = incident.rounds[0]
    lifecycle = InvestigationLifecycleService(database_session)
    lifecycle.start(first_round.id)
    hypothesis = Hypothesis(
        incident_id=incident.id,
        round_id=first_round.id,
        summary="第一轮假设。",
        status="CONFIRMED",
    )
    evidence = Evidence(
        incident_id=incident.id,
        round_id=first_round.id,
        evidence_type="metric",
        source="prometheus",
        content="第一轮证据。",
    )
    tool_call = ToolCall(
        incident_id=incident.id,
        round_id=first_round.id,
        tool_name="query_metrics",
        status="SUCCESS",
    )
    database_session.add_all((hypothesis, evidence, tool_call))
    database_session.commit()
    lifecycle.terminalize(first_round.id, InvestigationStatus.CONCLUDED, terminal_reason="first")
    report = Report(
        incident_id=incident.id,
        round_id=first_round.id,
        version=1,
        content={"schema_version": "v2", "round": 1},
    )
    database_session.add(report)
    database_session.commit()

    observation = Observation(
        content="第二轮需要验证的新观察。",
        observed_at=datetime(2026, 9, 19, 10, tzinfo=UTC),
    )
    second_round = InvestigationRoundService(database_session).create_next_round(
        incident.id, observation
    )
    database_session.refresh(incident)
    database_session.refresh(first_round)

    assert incident.investigation_status is InvestigationStatus.OPEN
    assert second_round.status is InvestigationStatus.OPEN
    assert observation.round_id == second_round.id
    assert observation.incident_id == incident.id
    assert first_round.status is InvestigationStatus.CONCLUDED
    assert first_round.terminal_reason == "first"
    assert first_round.report is report

    for record, field, value in (
        (hypothesis, "summary", "改写第一轮假设。"),
        (evidence, "content", "改写第一轮证据。"),
        (tool_call, "status", "FAILED"),
        (report, "content", {"schema_version": "v2", "round": "overwritten"}),
        (first_round, "terminal_reason", "overwritten"),
    ):
        setattr(record, field, value)
        with pytest.raises(ValueError, match="immutable"):
            database_session.commit()
        database_session.rollback()


def test_v2_second_round_persists_separate_explicitly_owned_records(
    database_session: Session,
) -> None:
    incident = _incident(database_session)
    first_round = incident.rounds[0]
    lifecycle = InvestigationLifecycleService(database_session)
    lifecycle.start(first_round.id)
    lifecycle.terminalize(first_round.id, InvestigationStatus.INCONCLUSIVE)
    first_report = Report(
        incident_id=incident.id,
        round_id=first_round.id,
        version=1,
        content={"round": 1},
    )
    database_session.add(first_report)
    database_session.commit()
    second_round = InvestigationRoundService(database_session).create_next_round(
        incident.id,
        Observation(
            content="继续调查的观察。",
            observed_at=datetime(2026, 9, 19, 10, tzinfo=UTC),
        ),
    )
    lifecycle.start(second_round.id)
    evidence = EvidenceContext(
        evidence_type="metric",
        source="prometheus",
        summary="第二轮指标异常。",
        data={"error_rate": 0.5},
    )
    hypothesis = HypothesisContext(
        summary="第二轮假设。",
        status=HypothesisStatus.CONFIRMED,
        supporting_evidence_ids=[evidence.id],
    )
    state = create_initial_agent_state(incident)
    state.update(
        {
            "round_id": second_round.id,
            "hypotheses": [hypothesis],
            "evidence": [evidence],
            "tool_history": [
                ToolHistoryEntry(
                    tool_name=ToolName.QUERY_METRICS,
                    status=ToolStatus.SUCCESS,
                    tool_arguments={"service": "order-service"},
                    evidence_ids=[evidence.id],
                )
            ],
        }
    )

    assert incident.thread_id == first_round.thread_id
    assert state["round_id"] == second_round.id
    V2Terminalizer(database_session).terminalize(state, InvestigationStatus.CONCLUDED)
    database_session.refresh(first_round)
    database_session.refresh(second_round)
    database_session.refresh(first_report)

    assert first_round.status is InvestigationStatus.INCONCLUSIVE
    assert first_report.content == {"round": 1}
    assert second_round.status is InvestigationStatus.CONCLUDED
    assert database_session.scalar(
        select(Hypothesis).where(Hypothesis.round_id == second_round.id)
    ) is not None
    assert database_session.scalar(
        select(Evidence).where(Evidence.round_id == second_round.id)
    ) is not None
    assert database_session.scalar(
        select(ToolCall).where(ToolCall.round_id == second_round.id)
    ) is not None
    second_report = database_session.scalar(
        select(Report).where(Report.round_id == second_round.id)
    )
    assert second_report is not None
    assert second_report.version == 2
