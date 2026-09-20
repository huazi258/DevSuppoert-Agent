"""Acceptance coverage for formal V2 supplemental-observation continuation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from devsupport_backend.agent.runtime import WorkflowCheckpointHistory, WorkflowFailure
from devsupport_backend.agent.state import (
    AgentState,
    EvidenceContext,
    FinalConclusion,
    HypothesisContext,
    HypothesisStatus,
    TerminalReason,
    create_initial_agent_state,
)
from devsupport_backend.agent.v2_terminalization import V2Terminalizer
from devsupport_backend.database import get_session
from devsupport_backend.investigation_continuation import InvestigationContinuationService
from devsupport_backend.investigation_lifecycle import (
    InvestigationLifecycleService,
    InvestigationRoundCreationError,
    current_round,
)
from devsupport_backend.investigation_status import InvestigationStatus
from devsupport_backend.main import app
from devsupport_backend.models import (
    Evidence,
    Hypothesis,
    Incident,
    InvestigationRound,
    InvestigationTarget,
    Observation,
    Report,
    Service,
    ToolCall,
)
from devsupport_backend.routers import incidents as incidents_router
from devsupport_backend.routers.incidents import get_workflow_runtime
from devsupport_backend.workflow_console import WorkflowConsoleService

NOW = datetime(2026, 9, 20, 8, tzinfo=UTC)


class _ContinuationRuntime:
    """Deterministic workflow boundary that persists an independent V2 terminal report."""

    def __init__(
        self,
        session: Session,
        terminal_status: InvestigationStatus = InvestigationStatus.INCONCLUSIVE,
    ) -> None:
        self._session = session
        self._terminal_status = terminal_status
        self.states: dict[str, AgentState] = {}
        self.started_threads: list[str] = []
        self.started_round_ids: list[UUID] = []
        self.started_symptoms: list[list[str]] = []

    def get_state(self, thread_id: str) -> AgentState | None:
        return self.states.get(thread_id)

    def get_failure(self, _: str) -> WorkflowFailure | None:
        return None

    def get_checkpoint_history(self, _: str) -> WorkflowCheckpointHistory:
        return WorkflowCheckpointHistory(records=())

    def start(self, incident: Incident) -> AgentState:
        round_record = current_round(self._session, incident.id)
        observations = list(
            self._session.scalars(
                select(Observation)
                .where(Observation.round_id == round_record.id)
                .order_by(Observation.observed_at)
            )
        )
        state = create_initial_agent_state(
            incident, symptoms=[observation.content for observation in observations]
        )
        state["round_id"] = round_record.id
        if self._terminal_status is InvestigationStatus.CONCLUDED:
            evidence = EvidenceContext(
                round_id=round_record.id,
                evidence_type="log",
                source="query_logs",
                summary="当前轮次发现订单服务配置缺失错误。",
            )
            state.update(
                {
                    "evidence": [evidence],
                    "hypotheses": [
                        HypothesisContext(
                            summary="订单服务配置缺失导致故障。",
                            status=HypothesisStatus.CONFIRMED,
                            supporting_evidence_ids=[evidence.id],
                        )
                    ],
                    "final_conclusion": FinalConclusion(
                        summary="当前运行日志确认订单服务配置缺失。",
                        root_cause="订单服务配置缺失。",
                        confidence=0.9,
                        supporting_evidence_ids=[evidence.id],
                    ),
                    "terminal_reason": TerminalReason.SUFFICIENT_EVIDENCE,
                }
            )
        else:
            state["terminal_reason"] = TerminalReason.NO_FURTHER_INVESTIGATION
        result = V2Terminalizer(self._session).terminalize(state, self._terminal_status)
        self.started_threads.append(round_record.thread_id)
        self.started_round_ids.append(round_record.id)
        self.started_symptoms.append(result["incident"].symptoms)
        self.states[round_record.thread_id] = result
        return result

    def retry_failed_task(self, _: str) -> AgentState:
        raise AssertionError("continuation acceptance does not retry a workflow")

    def record_retry_attempt(self, _: str) -> None:
        raise AssertionError("continuation acceptance does not retry a workflow")


def _open_incident(session: Session) -> tuple[Incident, InvestigationRound]:
    target = InvestigationTarget(
        name="Continuation acceptance target",
        slug=f"continuation-{uuid4().hex}",
        environment="local",
        enabled=True,
    )
    service = Service(
        target=target,
        name="order-service",
        display_name="Order service",
        enabled=True,
    )
    session.add(target)
    session.flush()
    incident = Incident(
        target_id=target.id,
        service_id=service.id,
        service=service.name,
        environment="local",
        description="订单接口持续返回 500。",
        time_range_start=NOW - timedelta(minutes=5),
        time_range_end=NOW,
        thread_id=str(uuid4()),
    )
    session.add(incident)
    session.commit()
    first_round = current_round(session, incident.id)
    return incident, first_round


def _terminal_incident(session: Session) -> tuple[Incident, InvestigationRound]:
    incident, first_round = _open_incident(session)
    lifecycle = InvestigationLifecycleService(session)
    lifecycle.start(first_round.id)
    session.add_all(
        (
            Hypothesis(
                incident_id=incident.id,
                round_id=first_round.id,
                summary="第一轮假设。",
                status="CONFIRMED",
            ),
            Evidence(
                incident_id=incident.id,
                round_id=first_round.id,
                evidence_type="metric",
                source="query_metrics",
                content="第一轮指标证据。",
            ),
            ToolCall(
                incident_id=incident.id,
                round_id=first_round.id,
                tool_name="query_metrics",
                status="SUCCESS",
            ),
        )
    )
    session.commit()
    lifecycle.terminalize(
        first_round.id,
        InvestigationStatus.CONCLUDED,
        terminal_reason="sufficient_evidence",
    )
    report = Report(
        incident_id=incident.id,
        round_id=first_round.id,
        version=1,
        content={"schema_version": "v2", "round": 1, "conclusion": "first"},
    )
    session.add(report)
    session.commit()
    return incident, first_round


def _round_snapshot(session: Session, round_record: InvestigationRound) -> dict[str, object]:
    report = session.scalar(select(Report).where(Report.round_id == round_record.id))
    assert report is not None
    return {
        "report": dict(report.content),
        "evidence_ids": set(
            session.scalars(select(Evidence.id).where(Evidence.round_id == round_record.id))
        ),
        "hypothesis_ids": set(
            session.scalars(select(Hypothesis.id).where(Hypothesis.round_id == round_record.id))
        ),
        "tool_call_ids": set(
            session.scalars(select(ToolCall.id).where(ToolCall.round_id == round_record.id))
        ),
    }


def test_terminal_observation_starts_an_isolated_round_and_workflow(
    database_session: Session,
) -> None:
    incident, first_round = _terminal_incident(database_session)
    before = _round_snapshot(database_session, first_round)
    runtime = _ContinuationRuntime(database_session)

    continuation = InvestigationContinuationService(
        database_session, runtime
    ).continue_with_observation(
        incident.id,
        "新增观察：订单服务的错误率仍在上升。",
        observed_at=NOW + timedelta(minutes=1),
    )
    second_round = database_session.get(InvestigationRound, continuation.round_id)
    assert second_round is not None
    assert continuation.previous_round_id == first_round.id
    assert continuation.round_number == 2
    assert continuation.status is InvestigationStatus.INVESTIGATING
    assert second_round.thread_id != first_round.thread_id
    assert incident.investigation_status is InvestigationStatus.INVESTIGATING
    observation = database_session.get(Observation, continuation.observation_id)
    assert observation is not None
    assert observation.incident_id == incident.id
    assert observation.round_id == second_round.id
    assert observation.content == continuation.observation_content

    WorkflowConsoleService(database_session, runtime).execute_accepted_start(incident.id)
    database_session.refresh(first_round)
    database_session.refresh(second_round)
    database_session.refresh(incident)

    assert runtime.started_threads == [second_round.thread_id]
    assert runtime.started_round_ids == [second_round.id]
    assert runtime.started_symptoms == [[observation.content]]
    assert incident.investigation_status is InvestigationStatus.INCONCLUSIVE
    assert second_round.status is InvestigationStatus.INCONCLUSIVE
    assert _round_snapshot(database_session, first_round) == before
    assert (
        database_session.scalar(
            select(func.count()).select_from(Evidence).where(Evidence.round_id == second_round.id)
        )
        == 0
    )
    assert (
        database_session.scalar(
            select(func.count())
            .select_from(Hypothesis)
            .where(Hypothesis.round_id == second_round.id)
        )
        == 0
    )
    assert (
        database_session.scalar(select(Report).where(Report.round_id == second_round.id))
        is not None
    )


def test_new_round_workflow_can_independently_conclude_without_old_evidence(
    database_session: Session,
) -> None:
    incident, first_round = _terminal_incident(database_session)
    first_evidence_ids = set(
        database_session.scalars(select(Evidence.id).where(Evidence.round_id == first_round.id))
    )
    runtime = _ContinuationRuntime(database_session, InvestigationStatus.CONCLUDED)
    continuation = InvestigationContinuationService(
        database_session, runtime
    ).continue_with_observation(incident.id, "新的运行时错误观察。")

    WorkflowConsoleService(database_session, runtime).execute_accepted_start(incident.id)
    second_round = database_session.get(InvestigationRound, continuation.round_id)
    assert second_round is not None
    second_evidence = list(
        database_session.scalars(select(Evidence).where(Evidence.round_id == second_round.id))
    )
    report = database_session.scalar(select(Report).where(Report.round_id == second_round.id))

    assert incident.investigation_status is InvestigationStatus.CONCLUDED
    assert second_round.status is InvestigationStatus.CONCLUDED
    assert report is not None and report.root_cause == "订单服务配置缺失。"
    assert {item.id for item in second_evidence}.isdisjoint(first_evidence_ids)
    assert all(item.round_id == second_round.id for item in second_evidence)


@pytest.mark.parametrize("status", [InvestigationStatus.OPEN, InvestigationStatus.INVESTIGATING])
def test_nonterminal_incident_cannot_create_a_continuation(
    database_session: Session, status: InvestigationStatus
) -> None:
    incident, first_round = _open_incident(database_session)
    if status is InvestigationStatus.INVESTIGATING:
        InvestigationLifecycleService(database_session).start(first_round.id)
    assert incident.investigation_status is status
    assert first_round.status is status

    with pytest.raises(InvestigationRoundCreationError):
        InvestigationContinuationService(
            database_session, _ContinuationRuntime(database_session)
        ).continue_with_observation(incident.id, "不应启动新的调查轮次。")


def test_multiple_terminal_continuations_form_distinct_round_history(
    database_session: Session,
) -> None:
    incident, first_round = _terminal_incident(database_session)
    runtime = _ContinuationRuntime(database_session)
    service = InvestigationContinuationService(database_session, runtime)

    second = service.continue_with_observation(incident.id, "第二轮观察。")
    second_round = database_session.get(InvestigationRound, second.round_id)
    assert second_round is not None
    InvestigationLifecycleService(database_session).terminalize(
        second_round.id,
        InvestigationStatus.FAILED,
        terminal_reason="repeated_failures",
    )
    third = service.continue_with_observation(incident.id, "第三轮观察。")
    third_round = database_session.get(InvestigationRound, third.round_id)
    assert third_round is not None

    assert [round_record.round_number for round_record in incident.rounds] == [1, 2, 3]
    assert [round_record.thread_id for round_record in incident.rounds] == [
        first_round.thread_id,
        second_round.thread_id,
        third_round.thread_id,
    ]
    assert len({round_record.thread_id for round_record in incident.rounds}) == 3
    assert third.previous_round_id == second_round.id
    assert third_round.status is InvestigationStatus.INVESTIGATING


def test_continuation_api_returns_only_new_round_and_observation_facts(
    database_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incident, first_round = _terminal_incident(database_session)
    runtime = _ContinuationRuntime(database_session)

    def override_get_session():
        yield database_session

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_workflow_runtime] = lambda: runtime
    monkeypatch.setattr(incidents_router, "execute_accepted_start", lambda _: None)
    try:
        with TestClient(app) as client:
            response = client.post(
                f"/incidents/{incident.id}/continuations",
                json={
                    "content": "API 补充观察。",
                    "observed_at": "2026-09-20T08:01:00+00:00",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202
    body = response.json()
    assert set(body) == {
        "incident_id",
        "round_id",
        "round_number",
        "status",
        "observation",
        "previous_round_id",
        "accepted",
    }
    assert body["round_number"] == 2
    assert body["status"] == "INVESTIGATING"
    assert body["previous_round_id"] == str(first_round.id)
    assert body["observation"]["content"] == "API 补充观察。"
    assert "remediation" not in body
    assert "recovery" not in body
