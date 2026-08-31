"""Unit coverage for the evaluator-only deterministic remediation regression contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from devsupport_backend.agent.state import (
    AgentStage,
    ApprovalStatus,
    TerminalReason,
    VerificationStatus,
)
from devsupport_backend.evals.remediation_regression import (
    DuplicateDecisionFact,
    RemediationActionFact,
    RemediationApprovalFact,
    RemediationDeploymentFact,
    RemediationRegressionArtifact,
    RemediationRegressionCase,
    RemediationRegressionCaseResult,
    RemediationRegressionStatus,
    RemediationVerificationFact,
    assess_remediation_case,
    assess_remediation_regression,
    create_grounded_concluded_state,
    validate_grounded_concluded_state,
    write_remediation_regression_artifact,
)
from devsupport_backend.models import Incident


def _result(case: RemediationRegressionCase) -> RemediationRegressionCaseResult:
    action_id = uuid4()
    approval_id = uuid4()
    before = RemediationDeploymentFact(current_version="v1.1.0", previous_version="v1.0.0")
    common = {
        "case": case,
        "started_at": datetime(2026, 8, 31, 12, tzinfo=UTC),
        "incident_id": uuid4(),
        "thread_id": "remediation-regression-thread",
        "policy_decision": "APPROVAL_REQUIRED",
        "policy_reason_code": "approval_required",
        "checkpoint_action_id": action_id,
        "action": RemediationActionFact(
            action_id=action_id,
            status=(
                "REJECTED"
                if case is RemediationRegressionCase.APPROVAL_REJECTED
                else "EXECUTED"
            ),
            current_version="v1.1.0",
            target_version="v1.0.0",
            executed=case is not RemediationRegressionCase.APPROVAL_REJECTED,
        ),
        "approval": RemediationApprovalFact(
            approval_id=approval_id,
            action_id=action_id,
            status=(
                ApprovalStatus.REJECTED
                if case is RemediationRegressionCase.APPROVAL_REJECTED
                else ApprovalStatus.APPROVED
            ),
        ),
        "action_count": 1,
        "approval_count": 1,
        "unauthorized_execution_count": 0,
        "deployment_before": before,
        "final_report_persisted": True,
        "cleanup_succeeded": True,
    }
    if case is RemediationRegressionCase.APPROVE_RECOVERY_PASS:
        return RemediationRegressionCaseResult(
            **common,
            verification=RemediationVerificationFact(
                verification_id=uuid4(), action_id=action_id, status=VerificationStatus.PASS
            ),
            verification_count=1,
            final_incident_status="RESOLVED",
            final_stage=AgentStage.RESOLVED,
            deployment_after=RemediationDeploymentFact(
                current_version="v1.0.0", previous_version="v1.1.0"
            ),
            duplicate_same_decision=DuplicateDecisionFact(
                accepted=True, same_approval_id=True, resume_required=False
            ),
            conflicting_decision_rejected=True,
        )
    if case is RemediationRegressionCase.APPROVAL_REJECTED:
        return RemediationRegressionCaseResult(
            **common,
            verification_count=0,
            final_incident_status="NEEDS_MANUAL_ACTION",
            final_stage=AgentStage.NEEDS_MANUAL_ACTION,
            terminal_reason=TerminalReason.APPROVAL_REJECTED,
            deployment_after=before,
            duplicate_same_decision=DuplicateDecisionFact(
                accepted=True, same_approval_id=True, resume_required=False
            ),
            conflicting_decision_rejected=True,
        )
    return RemediationRegressionCaseResult(
        **common,
        verification=RemediationVerificationFact(
            verification_id=uuid4(), action_id=action_id, status=VerificationStatus.FAIL
        ),
        verification_count=1,
        final_incident_status="NEEDS_MANUAL_ACTION",
        final_stage=AgentStage.NEEDS_MANUAL_ACTION,
        terminal_reason=TerminalReason.RECOVERY_VERIFICATION_FAILED,
        deployment_after=RemediationDeploymentFact(
            current_version="v1.0.0", previous_version="v1.1.0"
        ),
    )


def test_grounded_concluded_state_satisfies_policy_prerequisites() -> None:
    now = datetime.now(UTC)
    incident = Incident(
        id=uuid4(),
        service="order-service",
        environment="local",
        description="Order submissions are failing.",
        time_range_start=now,
        time_range_end=now + timedelta(minutes=1),
        thread_id=str(uuid4()),
    )

    state = create_grounded_concluded_state(incident)

    assert state["current_stage"] is AgentStage.CONCLUSION
    assert state["evaluation_decision"].value == "CONCLUDE"
    assert state["hypotheses"][0].status.value == "CONFIRMED"
    assert state["hypotheses"][0].supporting_evidence_ids == [state["evidence"][0].id]


@pytest.mark.parametrize("mutation", ["wrong_stage", "ungrounded_evidence"])
def test_invalid_prepared_state_is_not_silently_repaired(mutation: str) -> None:
    now = datetime.now(UTC)
    incident = Incident(
        id=uuid4(),
        service="order-service",
        environment="local",
        description="Order submissions are failing.",
        time_range_start=now,
        time_range_end=now + timedelta(minutes=1),
        thread_id=str(uuid4()),
    )
    state = create_grounded_concluded_state(incident)
    if mutation == "wrong_stage":
        state["current_stage"] = AgentStage.HYPOTHESIS_UPDATE
    else:
        state["hypotheses"][0] = state["hypotheses"][0].model_copy(
            update={"supporting_evidence_ids": [uuid4()]}
        )

    with pytest.raises(ValueError):
        validate_grounded_concluded_state(state)


@pytest.mark.parametrize("case", list(RemediationRegressionCase))
def test_happy_reject_and_recovery_failure_assessments_pass(
    case: RemediationRegressionCase,
) -> None:
    assessment = assess_remediation_case(_result(case))

    assert assessment.status is RemediationRegressionStatus.PASS
    assert assessment.failed_checks == []


def test_rejected_action_execution_fails_closed() -> None:
    result = _result(RemediationRegressionCase.APPROVAL_REJECTED)
    result.action = result.action.model_copy(update={"executed": True})  # type: ignore[union-attr]

    assessment = assess_remediation_case(result)

    assert assessment.status is RemediationRegressionStatus.FAIL
    assert "rejected_action_executed" in assessment.failed_checks


def test_unapproved_execution_and_action_approval_mismatch_fail() -> None:
    result = _result(RemediationRegressionCase.RECOVERY_VERIFICATION_FAILURE)
    result.approval = result.approval.model_copy(  # type: ignore[union-attr]
        update={"status": ApprovalStatus.REJECTED, "action_id": uuid4()}
    )
    result.unauthorized_execution_count = 1

    assessment = assess_remediation_case(result)

    assert assessment.status is RemediationRegressionStatus.FAIL
    assert "approval_action_binding" in assessment.failed_checks
    assert "unauthorized_execution" in assessment.failed_checks
    assert "approval_not_approved" in assessment.failed_checks


def test_checkpoint_action_mismatch_fails() -> None:
    result = _result(RemediationRegressionCase.APPROVE_RECOVERY_PASS)
    result.checkpoint_action_id = uuid4()

    assessment = assess_remediation_case(result)

    assert assessment.status is RemediationRegressionStatus.FAIL
    assert "checkpoint_action_binding" in assessment.failed_checks


def test_verification_bound_to_a_different_action_fails() -> None:
    result = _result(RemediationRegressionCase.APPROVE_RECOVERY_PASS)
    result.verification = result.verification.model_copy(update={"action_id": uuid4()})  # type: ignore[union-attr]

    assessment = assess_remediation_case(result)

    assert assessment.status is RemediationRegressionStatus.FAIL
    assert "verification_action_binding" in assessment.failed_checks


def test_duplicate_and_opposite_decision_requirements_are_machine_checked() -> None:
    result = _result(RemediationRegressionCase.APPROVE_RECOVERY_PASS)
    result.duplicate_same_decision = DuplicateDecisionFact(
        accepted=True, same_approval_id=True, resume_required=False
    )

    assert assess_remediation_case(result).status is RemediationRegressionStatus.PASS

    result.conflicting_decision_rejected = False
    assessment = assess_remediation_case(result)
    assert assessment.status is RemediationRegressionStatus.FAIL
    assert "opposite_approval_not_rejected" in assessment.failed_checks


def test_product_failure_takes_precedence_over_infrastructure_blocker() -> None:
    blocked = _result(RemediationRegressionCase.APPROVE_RECOVERY_PASS)
    blocked.infrastructure_error = "fault_lab_preparation_unavailable"
    assert assess_remediation_case(blocked).status is RemediationRegressionStatus.BLOCKED

    failed = blocked.model_copy(update={"product_error": "RuntimeError"})
    assert assess_remediation_case(failed).status is RemediationRegressionStatus.FAIL


def test_artifact_contract_is_safe_and_requires_exact_cases(tmp_path: Path) -> None:
    results = [
        assess_remediation_case(_result(case)) for case in RemediationRegressionCase
    ]
    artifact = RemediationRegressionArtifact(
        version="v1",
        run_started_at=datetime(2026, 8, 31, 12, tzinfo=UTC),
        cases=results,
        assessment=assess_remediation_regression(results),
    )
    output = tmp_path / "remediation-regression.json"
    write_remediation_regression_artifact(output, artifact)
    serialized = output.read_text(encoding="utf-8")

    assert artifact.assessment.status is RemediationRegressionStatus.PASS
    assert "raw_logs" not in serialized
    assert "tool_arguments" not in serialized
    assert "approval_interrupt_payload" not in serialized
    with pytest.raises(ValidationError):
        RemediationRegressionCaseResult.model_validate(
            {**results[0].model_dump(mode="json"), "raw_logs": ["forbidden"]}
        )
