"""Task 4.6 final report facts are typed, bound, and idempotent."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from devsupport_backend.agent.state import (
    ActionExecutionOutcome,
    ActionType,
    ApprovalOutcome,
    ApprovalStatus,
    EvidenceContext,
    FinalConclusion,
    HypothesisContext,
    HypothesisStatus,
    PolicyDecision,
    PolicyOutcome,
    PolicyReasonCode,
    VerificationOutcome,
    VerificationStatus,
    create_initial_agent_state,
)
from devsupport_backend.final_report import FinalReportError, FinalReportService
from devsupport_backend.models import Action, Approval, Incident, Report, Verification
from devsupport_backend.tools.schemas import ToolStatus


def _incident(session: Session, status: str) -> Incident:
    now = datetime.now(UTC)
    incident = Incident(
        service="order-service",
        environment="local",
        status=status,
        description="A controlled final report test incident.",
        time_range_start=now,
        time_range_end=now + timedelta(minutes=5),
        thread_id=str(uuid4()),
    )
    session.add(incident)
    session.commit()
    return incident


def _state(incident: Incident):
    evidence = EvidenceContext(
        evidence_type="metric_snapshot",
        source="query_metrics",
        summary="The error rate increased after deployment.",
        data={"error_count": 1},
    )
    hypothesis = HypothesisContext(
        summary="The current deployment has a missing configuration.",
        status=HypothesisStatus.CONFIRMED,
        confidence=0.9,
        supporting_evidence_ids=[evidence.id],
    )
    state = create_initial_agent_state(incident)
    state.update(
        {
            "hypotheses": [hypothesis],
            "evidence": [evidence],
            "final_conclusion": FinalConclusion(
                summary="The evidence supports a deployment configuration failure.",
                root_cause=hypothesis.summary,
                confidence=0.9,
                supporting_evidence_ids=[evidence.id],
            ),
        }
    )
    return state


def test_manual_report_has_no_remediation_facts_and_is_idempotent(
    database_session: Session,
) -> None:
    incident = _incident(database_session, "NEEDS_MANUAL_ACTION")
    state = _state(incident)
    service = FinalReportService(database_session)

    first = service.generate(state)
    second = service.generate(state)
    report = database_session.get(Report, first.report_id)

    assert second.report_id == first.report_id
    assert report is not None
    assert report.content["final_status"] == "NEEDS_MANUAL_ACTION"
    assert report.content["action"] is None
    assert report.content["approval"] is None
    assert report.content["execution"] is None
    assert report.content["verification"] is None
    assert database_session.scalar(select(func.count()).select_from(Report)) == 1


def test_resolved_report_persists_exact_execution_chain_as_jsonb(database_session: Session) -> None:
    incident = _incident(database_session, "RESOLVED")
    state = _state(incident)
    action = Action(
        incident_id=incident.id,
        action_type=ActionType.ROLLBACK_DEPLOYMENT.value,
        status="EXECUTED",
        parameters={
            "service": "order-service",
            "environment": "local",
            "current_version": "v1.1.0",
            "target_version": "v1.0.0",
            "reason": "Evidence supports rollback.",
        },
        executed_at=datetime.now(UTC),
    )
    database_session.add(action)
    database_session.commit()
    approval = Approval(incident_id=incident.id, action_id=action.id, status="APPROVED")
    verification = Verification(
        incident_id=incident.id,
        action_id=action.id,
        status="PASS",
        summary="Post-rollback checks passed.",
        details={"verification_completed_at": datetime.now(UTC).isoformat()},
    )
    database_session.add_all([approval, verification])
    database_session.commit()
    state.update(
        {
            "policy_outcome": PolicyOutcome(
                decision=PolicyDecision.APPROVAL_REQUIRED,
                reason_code=PolicyReasonCode.APPROVAL_REQUIRED,
                reason="Rollback requires approval.",
                action_id=action.id,
            ),
            "approval_outcome": ApprovalOutcome(
                approval_id=approval.id,
                action_id=action.id,
                status=ApprovalStatus.APPROVED,
            ),
            "execution_outcome": ActionExecutionOutcome(
                action_id=action.id,
                approval_id=approval.id,
                status=ToolStatus.SUCCESS,
                service="order-service",
                environment="local",
                target_version="v1.0.0",
                executed=True,
            ),
            "verification_outcome": VerificationOutcome(
                verification_id=verification.id,
                action_id=action.id,
                status=VerificationStatus.PASS,
                summary="Post-rollback checks passed.",
            ),
        }
    )

    outcome = FinalReportService(database_session).generate(state)
    report = database_session.get(Report, outcome.report_id)

    assert report is not None
    assert report.content["action"]["action_id"] == str(action.id)
    assert report.content["approval"]["approval_id"] == str(approval.id)
    assert report.content["execution"]["target_version"] == "v1.0.0"
    assert report.content["verification"]["verification_id"] == str(verification.id)
    assert report.content["key_evidence"][0]["id"] == str(state["evidence"][0].id)
    assert any(item["type"] == "verification_completed" for item in report.content["timeline"])


def test_existing_report_conflict_fails_closed_without_second_row(
    database_session: Session,
) -> None:
    incident = _incident(database_session, "NEEDS_MANUAL_ACTION")
    state = _state(incident)
    service = FinalReportService(database_session)
    outcome = service.generate(state)
    report = database_session.get(Report, outcome.report_id)
    assert report is not None
    report.content = {**report.content, "final_status": "RESOLVED"}
    database_session.commit()

    with pytest.raises(FinalReportError, match="conflicts"):
        service.generate(state)

    assert database_session.scalar(select(func.count()).select_from(Report)) == 1


def test_unknown_evidence_reference_cannot_be_reported(database_session: Session) -> None:
    incident = _incident(database_session, "NEEDS_MANUAL_ACTION")
    state = _state(incident)
    state["final_conclusion"] = state["final_conclusion"].model_copy(
        update={"supporting_evidence_ids": [uuid4()]}
    )

    with pytest.raises(FinalReportError, match="unknown Evidence"):
        FinalReportService(database_session).generate(state)

    assert database_session.scalar(select(func.count()).select_from(Report)) == 0


@pytest.mark.parametrize("field", ["action_id", "approval_id", "target_version"])
def test_failed_execution_non_null_facts_must_bind_to_authoritative_records(
    database_session: Session, field: str
) -> None:
    incident = _incident(database_session, "NEEDS_MANUAL_ACTION")
    state = _state(incident)
    action = Action(
        incident_id=incident.id,
        action_type=ActionType.ROLLBACK_DEPLOYMENT.value,
        status="FAILED",
        parameters={
            "service": "order-service",
            "environment": "local",
            "current_version": "v1.1.0",
            "target_version": "v1.0.0",
            "reason": "Evidence supports rollback.",
        },
    )
    database_session.add(action)
    database_session.commit()
    approval = Approval(incident_id=incident.id, action_id=action.id, status="APPROVED")
    database_session.add(approval)
    database_session.commit()
    state.update(
        {
            "policy_outcome": PolicyOutcome(
                decision=PolicyDecision.APPROVAL_REQUIRED,
                reason_code=PolicyReasonCode.APPROVAL_REQUIRED,
                reason="Rollback requires approval.",
                action_id=action.id,
            ),
            "approval_outcome": ApprovalOutcome(
                approval_id=approval.id,
                action_id=action.id,
                status=ApprovalStatus.APPROVED,
            ),
            "execution_outcome": ActionExecutionOutcome(
                action_id=uuid4() if field == "action_id" else action.id,
                approval_id=uuid4() if field == "approval_id" else approval.id,
                status=ToolStatus.FAILURE,
                service="order-service",
                environment="local",
                target_version="v9.9.9" if field == "target_version" else "v1.0.0",
                executed=False,
            ),
        }
    )

    with pytest.raises(FinalReportError, match="Failed execution"):
        FinalReportService(database_session).generate(state)


def test_rejected_approval_produces_manual_report(database_session: Session) -> None:
    incident = _incident(database_session, "NEEDS_MANUAL_ACTION")
    state = _state(incident)
    action = Action(
        incident_id=incident.id,
        action_type=ActionType.ROLLBACK_DEPLOYMENT.value,
        status="REJECTED",
        parameters={
            "service": "order-service",
            "environment": "local",
            "current_version": "v1.1.0",
            "target_version": "v1.0.0",
            "reason": "Evidence supports rollback.",
        },
    )
    database_session.add(action)
    database_session.commit()
    approval = Approval(incident_id=incident.id, action_id=action.id, status="REJECTED")
    database_session.add(approval)
    database_session.commit()
    state.update(
        {
            "policy_outcome": PolicyOutcome(
                decision=PolicyDecision.APPROVAL_REQUIRED,
                reason_code=PolicyReasonCode.APPROVAL_REQUIRED,
                reason="Rollback requires approval.",
                action_id=action.id,
            ),
            "approval_outcome": ApprovalOutcome(
                approval_id=approval.id,
                action_id=action.id,
                status=ApprovalStatus.REJECTED,
            ),
        }
    )

    outcome = FinalReportService(database_session).generate(state)
    report = database_session.get(Report, outcome.report_id)

    assert report is not None
    assert report.content["approval"]["status"] == "REJECTED"
    assert report.content["execution"] is None
    assert report.content["verification"] is None


@pytest.mark.parametrize("verification_status", ["FAIL", "INCONCLUSIVE"])
def test_manual_verification_outcomes_produce_a_report(
    database_session: Session, verification_status: str
) -> None:
    incident = _incident(database_session, "NEEDS_MANUAL_ACTION")
    state = _state(incident)
    action = Action(
        incident_id=incident.id,
        action_type=ActionType.ROLLBACK_DEPLOYMENT.value,
        status="EXECUTED",
        parameters={
            "service": "order-service",
            "environment": "local",
            "current_version": "v1.1.0",
            "target_version": "v1.0.0",
            "reason": "Evidence supports rollback.",
        },
        executed_at=datetime.now(UTC),
    )
    database_session.add(action)
    database_session.commit()
    approval = Approval(incident_id=incident.id, action_id=action.id, status="APPROVED")
    verification = Verification(
        incident_id=incident.id,
        action_id=action.id,
        status=verification_status,
        summary="Recovery requires manual follow-up.",
        details={},
    )
    database_session.add_all([approval, verification])
    database_session.commit()
    state.update(
        {
            "policy_outcome": PolicyOutcome(
                decision=PolicyDecision.APPROVAL_REQUIRED,
                reason_code=PolicyReasonCode.APPROVAL_REQUIRED,
                reason="Rollback requires approval.",
                action_id=action.id,
            ),
            "approval_outcome": ApprovalOutcome(
                approval_id=approval.id,
                action_id=action.id,
                status=ApprovalStatus.APPROVED,
            ),
            "execution_outcome": ActionExecutionOutcome(
                action_id=action.id,
                approval_id=approval.id,
                status=ToolStatus.SUCCESS,
                service="order-service",
                environment="local",
                target_version="v1.0.0",
                executed=True,
            ),
            "verification_outcome": VerificationOutcome(
                verification_id=verification.id,
                action_id=action.id,
                status=VerificationStatus(verification_status),
                summary=verification.summary,
            ),
        }
    )

    outcome = FinalReportService(database_session).generate(state)

    assert outcome.final_status == "NEEDS_MANUAL_ACTION"
    assert database_session.get(Report, outcome.report_id) is not None


def test_postgres_rejects_second_report_for_the_same_incident(database_session: Session) -> None:
    incident = _incident(database_session, "NEEDS_MANUAL_ACTION")
    database_session.add(Report(incident_id=incident.id, content={"first": True}))
    database_session.commit()
    database_session.add(Report(incident_id=incident.id, content={"second": True}))

    with pytest.raises(IntegrityError):
        database_session.commit()
    database_session.rollback()
