"""Deterministic post-conclusion regression for the production remediation chain."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from devsupport_backend.action_execution import ActionExecutionService
from devsupport_backend.agent.persistence import open_postgres_checkpointer
from devsupport_backend.agent.policy import PolicyGateService
from devsupport_backend.agent.runtime import WorkflowService
from devsupport_backend.agent.state import (
    ActionType,
    AgentStage,
    AgentState,
    ApprovalStatus,
    EvaluationDecision,
    EvidenceContext,
    FinalConclusion,
    HypothesisContext,
    HypothesisStatus,
    PolicyDecision,
    PolicyReasonCode,
    ProposedAction,
    TerminalReason,
    VerificationStatus,
    create_initial_agent_state,
)
from devsupport_backend.approvals import (
    ApprovalDecisionConflict,
    ApprovalDecisionService,
    ApprovalService,
    ApprovalWaitService,
    PostgresApprovalWorkflowCoordinator,
    PostgresWorkflowStateReader,
    approval_interrupt_node,
    approval_wait_node,
    build_approval_resume_graph,
)
from devsupport_backend.database import SessionLocal
from devsupport_backend.evals.contracts import EvalFixture, load_eval_fixture_suite
from devsupport_backend.evals.runner import (
    DEFAULT_SUITE_PATH,
    EvalRunnerError,
    LiveFaultLabController,
)
from devsupport_backend.final_report import FinalReportService
from devsupport_backend.models import Action, Approval, Incident, Report, Verification
from devsupport_backend.recovery_verification import RecoveryVerificationService
from devsupport_backend.schemas.approvals import ApprovalDecision
from devsupport_backend.tools.deployments import FaultLabDeploymentAdapter, FaultLabRollbackAdapter
from devsupport_backend.tools.logs import FaultLabLogsAdapter
from devsupport_backend.tools.metrics import FaultLabMetricsAdapter
from devsupport_backend.tools.recovery_probe import RecoveryProbeResult

DEFAULT_ARTIFACT_PATH = (
    Path(__file__).resolve().parents[5]
    / "evals"
    / "results"
    / "v1-m5.4-approval-recovery-regression.json"
)
_MISSING_CONFIG_FIXTURE_ID = "a-approve-happy"


class RemediationRegressionStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class RemediationRegressionCase(StrEnum):
    APPROVE_RECOVERY_PASS = "approve_recovery_pass"
    APPROVAL_REJECTED = "approval_rejected"
    RECOVERY_VERIFICATION_FAILURE = "recovery_verification_failure"


class _StrictRemediationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_assignment=True)


class RemediationDeploymentFact(_StrictRemediationModel):
    current_version: str = Field(min_length=1, max_length=100)
    previous_version: str | None = Field(default=None, max_length=100)


class RemediationActionFact(_StrictRemediationModel):
    action_id: UUID
    status: str = Field(min_length=1, max_length=50)
    current_version: str = Field(min_length=1, max_length=100)
    target_version: str = Field(min_length=1, max_length=100)
    executed: bool


class RemediationApprovalFact(_StrictRemediationModel):
    approval_id: UUID
    action_id: UUID
    status: ApprovalStatus


class RemediationVerificationFact(_StrictRemediationModel):
    verification_id: UUID
    action_id: UUID
    status: VerificationStatus


class DuplicateDecisionFact(_StrictRemediationModel):
    accepted: bool
    same_approval_id: bool
    resume_required: bool | None = None


class RemediationRegressionCaseResult(_StrictRemediationModel):
    case: RemediationRegressionCase
    started_at: datetime
    incident_id: UUID | None = None
    thread_id: str | None = Field(default=None, min_length=1, max_length=255)
    policy_decision: PolicyDecision | None = None
    policy_reason_code: PolicyReasonCode | None = None
    checkpoint_action_id: UUID | None = None
    action: RemediationActionFact | None = None
    approval: RemediationApprovalFact | None = None
    verification: RemediationVerificationFact | None = None
    final_incident_status: str | None = Field(default=None, min_length=1, max_length=50)
    terminal_reason: TerminalReason | None = None
    final_stage: AgentStage | None = None
    final_report_persisted: bool = False
    action_count: int = Field(default=0, ge=0)
    approval_count: int = Field(default=0, ge=0)
    verification_count: int = Field(default=0, ge=0)
    unauthorized_execution_count: int = Field(default=0, ge=0)
    deployment_before: RemediationDeploymentFact | None = None
    deployment_after: RemediationDeploymentFact | None = None
    duplicate_same_decision: DuplicateDecisionFact | None = None
    conflicting_decision_rejected: bool | None = None
    cleanup_succeeded: bool | None = None
    infrastructure_error: str | None = Field(default=None, min_length=1, max_length=200)
    product_error: str | None = Field(default=None, min_length=1, max_length=200)
    status: RemediationRegressionStatus | None = None
    failed_checks: list[str] = Field(default_factory=list, max_length=30)


class RemediationRegressionAssessment(_StrictRemediationModel):
    status: RemediationRegressionStatus
    failed_cases: list[RemediationRegressionCase] = Field(default_factory=list, max_length=3)
    blocked_cases: list[RemediationRegressionCase] = Field(default_factory=list, max_length=3)


class RemediationRegressionArtifact(_StrictRemediationModel):
    version: Literal["v1"]
    run_started_at: datetime
    cases: list[RemediationRegressionCaseResult] = Field(min_length=3, max_length=3)
    assessment: RemediationRegressionAssessment

    @model_validator(mode="after")
    def require_each_fixed_case_once(self) -> "RemediationRegressionArtifact":
        if {result.case for result in self.cases} != set(RemediationRegressionCase):
            raise ValueError("artifact must contain each fixed remediation regression case once")
        return self


def create_grounded_concluded_state(incident: Incident) -> AgentState:
    """Build evaluator-owned, internally consistent post-investigation facts only."""
    evidence = EvidenceContext(
        evidence_type="log_query_result",
        source="query_logs",
        summary="Order-service has a confirmed post-release configuration failure.",
        data={"pattern": "MissingRequiredConfiguration"},
    )
    hypothesis = HypothesisContext(
        summary="The current order-service release is missing required configuration.",
        status=HypothesisStatus.CONFIRMED,
        confidence=0.95,
        supporting_evidence_ids=[evidence.id],
    )
    state = create_initial_agent_state(incident, symptoms=["POST /orders returns 500"])
    state.update(
        {
            "current_stage": AgentStage.CONCLUSION,
            "evidence": [evidence],
            "hypotheses": [hypothesis],
            "evaluation_decision": EvaluationDecision.CONCLUDE,
            "final_conclusion": FinalConclusion(
                summary="Grounded evidence confirms a release configuration failure.",
                root_cause=hypothesis.summary,
                confidence=hypothesis.confidence,
                supporting_evidence_ids=[evidence.id],
                recommended_next_action="Request a policy-controlled rollback.",
            ),
            "proposed_action": ProposedAction(
                action_type=ActionType.ROLLBACK_DEPLOYMENT,
                summary="Recommend a policy-controlled rollback.",
                reason="Confirmed grounded evidence supports restoring the previous version.",
                risk="Rollback requires real human approval.",
                supporting_evidence_ids=[evidence.id],
            ),
        }
    )
    validate_grounded_concluded_state(state)
    return state


def validate_grounded_concluded_state(state: AgentState) -> None:
    """Reject invalid prepared state rather than repairing it inside the harness."""
    conclusion = state.get("final_conclusion")
    proposal = state.get("proposed_action")
    evidence_ids = {item.id for item in state.get("evidence", [])}
    confirmed = [
        item
        for item in state.get("hypotheses", [])
        if item.status is HypothesisStatus.CONFIRMED
    ]
    if (
        state.get("current_stage") is not AgentStage.CONCLUSION
        or state.get("evaluation_decision") is not EvaluationDecision.CONCLUDE
        or conclusion is None
        or proposal is None
        or proposal.action_type is not ActionType.ROLLBACK_DEPLOYMENT
        or proposal.parameters
        or not confirmed
    ):
        raise ValueError("prepared state is not a concluded rollback proposal")
    references = set(conclusion.supporting_evidence_ids) | set(proposal.supporting_evidence_ids)
    references.update(
        evidence_id for item in confirmed for evidence_id in item.supporting_evidence_ids
    )
    if not references or not references <= evidence_ids:
        raise ValueError("prepared state has ungrounded conclusion or hypothesis evidence")


def assess_remediation_case(
    result: RemediationRegressionCaseResult,
) -> RemediationRegressionCaseResult:
    """Classify only deterministic production-chain facts; no provider blocker exists here."""
    failures = _common_failures(result)
    if result.case is RemediationRegressionCase.APPROVE_RECOVERY_PASS:
        failures.extend(_approve_pass_failures(result))
    elif result.case is RemediationRegressionCase.APPROVAL_REJECTED:
        failures.extend(_reject_failures(result))
    else:
        failures.extend(_recovery_failure_failures(result))
    failures = list(dict.fromkeys(failures))
    if failures or result.product_error is not None:
        status = RemediationRegressionStatus.FAIL
    elif result.infrastructure_error is not None or result.cleanup_succeeded is False:
        status = RemediationRegressionStatus.BLOCKED
    else:
        status = RemediationRegressionStatus.PASS
    return result.model_copy(update={"status": status, "failed_checks": failures})


def assess_remediation_regression(
    results: list[RemediationRegressionCaseResult],
) -> RemediationRegressionAssessment:
    assessed = [assess_remediation_case(result) for result in results]
    failures = [
        result.case for result in assessed if result.status is RemediationRegressionStatus.FAIL
    ]
    blocked = [
        result.case for result in assessed if result.status is RemediationRegressionStatus.BLOCKED
    ]
    return RemediationRegressionAssessment(
        status=(
            RemediationRegressionStatus.FAIL
            if failures
            else RemediationRegressionStatus.BLOCKED
            if blocked
            else RemediationRegressionStatus.PASS
        ),
        failed_cases=failures,
        blocked_cases=blocked,
    )


class RemediationRegressionHarness:
    """Evaluator-only composition around the existing post-approval production boundaries."""

    def __init__(self, fault_lab: LiveFaultLabController | None = None) -> None:
        self._fault_lab = fault_lab or LiveFaultLabController()

    def run(self) -> RemediationRegressionArtifact:
        run_started_at = datetime.now(UTC)
        results: list[RemediationRegressionCaseResult] = []
        for case in RemediationRegressionCase:
            result = self._run_case(case)
            results.append(assess_remediation_case(result))
        return RemediationRegressionArtifact(
            version="v1",
            run_started_at=run_started_at,
            cases=results,
            assessment=assess_remediation_regression(results),
        )

    def _run_case(self, case: RemediationRegressionCase) -> RemediationRegressionCaseResult:
        result = RemediationRegressionCaseResult(case=case, started_at=datetime.now(UTC))
        try:
            self._fault_lab.reset()
            self._fault_lab.inject(_missing_config_fixture())
            self._execute_case(result)
        except EvalRunnerError:
            result.infrastructure_error = "fault_lab_preparation_unavailable"
        except SQLAlchemyError:
            result.infrastructure_error = "postgresql_or_checkpointer_unavailable"
        except Exception as error:
            result.product_error = type(error).__name__
        finally:
            try:
                self._fault_lab.reset()
                result.cleanup_succeeded = True
            except EvalRunnerError:
                result.cleanup_succeeded = False
        return result

    def _execute_case(self, result: RemediationRegressionCaseResult) -> None:
        with SessionLocal() as session:
            deployments = FaultLabDeploymentAdapter.from_settings()
            before = deployments.query(_deployment_input())
            result.deployment_before = RemediationDeploymentFact(
                current_version=before.current_version,
                previous_version=before.previous_version,
            )
            if before.previous_version is None or before.previous_version == before.current_version:
                raise RuntimeError("invalid_fault_lab_deployment_state")
            incident = _create_incident(session)
            result.incident_id = incident.id
            result.thread_id = incident.thread_id
            state = create_grounded_concluded_state(incident)
            outcome = PolicyGateService(session, deployments).evaluate(state)
            result.policy_decision = outcome.decision
            result.policy_reason_code = outcome.reason_code
            if (
                outcome.decision is not PolicyDecision.APPROVAL_REQUIRED
                or outcome.action_id is None
            ):
                raise RuntimeError("policy_gate_did_not_prepare_rollback")
            state.update({"policy_outcome": outcome, "current_stage": AgentStage.POLICY_GATE})
            _persist_waiting_approval_checkpoint(session, state, incident.thread_id)
        approval_result = _record_decision(result.incident_id, _decision_for(result.case))
        if result.case is RemediationRegressionCase.RECOVERY_VERIFICATION_FAILURE:
            _resume_with_failing_probe(result.thread_id)
        else:
            PostgresApprovalWorkflowCoordinator().resume(result.thread_id)
        _collect_result_facts(result)
        if result.case is not RemediationRegressionCase.RECOVERY_VERIFICATION_FAILURE:
            _record_duplicate_and_conflict(result, _decision_for(result.case))
            _collect_result_facts(result)
        action_id = result.action.action_id if result.action else None
        if approval_result.approval.action_id != action_id:
            raise RuntimeError("approval_action_binding_mismatch")


def write_remediation_regression_artifact(
    path: Path, artifact: RemediationRegressionArtifact
) -> None:
    path.write_text(
        json.dumps(artifact.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _common_failures(result: RemediationRegressionCaseResult) -> list[str]:
    failures: list[str] = []
    if result.policy_decision is not PolicyDecision.APPROVAL_REQUIRED:
        failures.append("policy_not_approval_required")
    if result.policy_reason_code is not PolicyReasonCode.APPROVAL_REQUIRED:
        failures.append("policy_reason_code")
    if result.action_count != 1 or result.action is None:
        failures.append("exactly_one_action")
    if result.approval_count != 1 or result.approval is None:
        failures.append("exactly_one_approval")
    if result.action is not None and result.approval is not None and (
        result.approval.action_id != result.action.action_id
    ):
        failures.append("approval_action_binding")
    if result.action is not None and result.checkpoint_action_id != result.action.action_id:
        failures.append("checkpoint_action_binding")
    if result.action is not None and result.deployment_before is not None and (
        result.action.current_version != result.deployment_before.current_version
        or result.action.target_version != result.deployment_before.previous_version
    ):
        failures.append("policy_deployment_parameters")
    if result.unauthorized_execution_count != 0:
        failures.append("unauthorized_execution")
    if not result.final_report_persisted:
        failures.append("final_report_missing")
    if result.deployment_before is None or result.deployment_after is None:
        failures.append("deployment_facts_missing")
    if result.product_error is not None:
        failures.append("product_exception")
    return failures


def _approve_pass_failures(result: RemediationRegressionCaseResult) -> list[str]:
    failures: list[str] = []
    if result.approval is None or result.approval.status is not ApprovalStatus.APPROVED:
        failures.append("approval_not_approved")
    if result.action is None or result.action.status != "EXECUTED":
        failures.append("action_not_executed")
    if result.verification_count != 1 or result.verification is None:
        failures.append("verification_missing")
    elif result.verification.status is not VerificationStatus.PASS:
        failures.append("verification_not_pass")
    elif result.action is not None and result.verification.action_id != result.action.action_id:
        failures.append("verification_action_binding")
    if result.final_incident_status != "RESOLVED" or result.final_stage is not AgentStage.RESOLVED:
        failures.append("resolved_terminal_state")
    if result.terminal_reason is not None:
        failures.append("unexpected_terminal_reason")
    if result.action and result.deployment_after and (
        result.deployment_after.current_version != result.action.target_version
    ):
        failures.append("rollback_target_not_live")
    if result.duplicate_same_decision is None or not (
        result.duplicate_same_decision.accepted
        and result.duplicate_same_decision.same_approval_id
    ):
        failures.append("duplicate_approval_not_idempotent")
    if result.conflicting_decision_rejected is not True:
        failures.append("opposite_approval_not_rejected")
    return failures


def _reject_failures(result: RemediationRegressionCaseResult) -> list[str]:
    failures: list[str] = []
    if result.approval is None or result.approval.status is not ApprovalStatus.REJECTED:
        failures.append("approval_not_rejected")
    if result.action is None or result.action.status != "REJECTED" or result.action.executed:
        failures.append("rejected_action_executed")
    if result.verification_count != 0 or result.verification is not None:
        failures.append("rejected_action_has_verification")
    if result.final_incident_status != "NEEDS_MANUAL_ACTION":
        failures.append("rejected_incident_terminal")
    if result.final_stage is not AgentStage.NEEDS_MANUAL_ACTION:
        failures.append("rejected_terminal_stage")
    if result.terminal_reason is not TerminalReason.APPROVAL_REJECTED:
        failures.append("rejected_terminal_reason")
    if result.deployment_before != result.deployment_after:
        failures.append("rejected_action_changed_deployment")
    if result.duplicate_same_decision is None or not (
        result.duplicate_same_decision.accepted
        and result.duplicate_same_decision.same_approval_id
    ):
        failures.append("duplicate_rejection_not_idempotent")
    if result.conflicting_decision_rejected is not True:
        failures.append("opposite_rejection_not_rejected")
    return failures


def _recovery_failure_failures(result: RemediationRegressionCaseResult) -> list[str]:
    failures: list[str] = []
    if result.approval is None or result.approval.status is not ApprovalStatus.APPROVED:
        failures.append("approval_not_approved")
    if result.action is None or result.action.status != "EXECUTED":
        failures.append("action_not_executed")
    if result.verification_count != 1 or result.verification is None:
        failures.append("verification_missing")
    elif result.verification.status is not VerificationStatus.FAIL:
        failures.append("verification_not_fail")
    elif result.action is not None and result.verification.action_id != result.action.action_id:
        failures.append("verification_action_binding")
    if result.final_incident_status != "NEEDS_MANUAL_ACTION":
        failures.append("recovery_failure_incident_terminal")
    if result.final_stage is not AgentStage.NEEDS_MANUAL_ACTION:
        failures.append("recovery_failure_terminal_stage")
    if result.terminal_reason is not TerminalReason.RECOVERY_VERIFICATION_FAILED:
        failures.append("recovery_failure_terminal_reason")
    if result.action and result.deployment_after and (
        result.deployment_after.current_version != result.action.target_version
    ):
        failures.append("rollback_target_not_live")
    return failures


def _missing_config_fixture() -> EvalFixture:
    suite = load_eval_fixture_suite(DEFAULT_SUITE_PATH)
    fixture = next(item for item in suite.fixtures if item.id == _MISSING_CONFIG_FIXTURE_ID)
    if not isinstance(fixture, EvalFixture):
        raise RuntimeError("missing_config_fixture_invalid")
    return fixture


def _deployment_input():
    from devsupport_backend.tools.schemas import GetDeploymentHistoryInput

    return GetDeploymentHistoryInput(service="order-service", environment="local")


def _create_incident(session: Session) -> Incident:
    now = datetime.now(UTC)
    incident = Incident(
        id=uuid4(),
        service="order-service",
        environment="local",
        status="OPEN",
        description="Order submissions are failing after a local release.",
        time_range_start=now - timedelta(minutes=5),
        time_range_end=now + timedelta(minutes=1),
        thread_id=str(uuid4()),
    )
    session.add(incident)
    session.commit()
    session.refresh(incident)
    return incident


def _persist_waiting_approval_checkpoint(
    session: Session, state: AgentState, thread_id: str
) -> None:
    approval_wait = ApprovalWaitService(session)
    graph = StateGraph(AgentState)
    graph.add_node("approval_wait", lambda current: approval_wait_node(current, approval_wait))
    graph.add_node(
        "approval_interrupt", lambda current: approval_interrupt_node(current, approval_wait)
    )
    graph.add_edge(START, "approval_wait")
    graph.add_edge("approval_wait", "approval_interrupt")
    graph.add_edge("approval_interrupt", END)
    with open_postgres_checkpointer() as checkpointer:
        compiled = graph.compile(checkpointer=checkpointer)
        interrupted = compiled.invoke(state, WorkflowService.config_for(thread_id))
    if "__interrupt__" not in interrupted:
        raise RuntimeError("approval_interrupt_not_persisted")


def _record_decision(incident_id: UUID | None, decision: ApprovalDecision):
    if incident_id is None:
        raise RuntimeError("incident_missing")
    with SessionLocal() as session:
        service = ApprovalService(session, PostgresWorkflowStateReader())
        return service.record_decision(incident_id, decision)


def _resume_with_failing_probe(thread_id: str | None) -> AgentState:
    if thread_id is None:
        raise RuntimeError("thread_missing")
    with SessionLocal() as session, open_postgres_checkpointer() as checkpointer:
        deployments = FaultLabDeploymentAdapter.from_settings()
        graph = build_approval_resume_graph(
            approval_wait=ApprovalWaitService(session),
            approval_decision=ApprovalDecisionService(session),
            action_execution=ActionExecutionService(
                session, deployments, FaultLabRollbackAdapter.from_settings()
            ),
            recovery_verification=RecoveryVerificationService(
                session,
                deployments,
                FaultLabMetricsAdapter.from_settings(),
                FaultLabLogsAdapter.from_settings(),
                _FailingRecoveryProbe(),
            ),
            final_report=FinalReportService(session),
            checkpointer=checkpointer,
        )
        return WorkflowService(graph).resume(thread_id, {"event": "approval_recorded"})


class _FailingRecoveryProbe:
    def probe(self) -> RecoveryProbeResult:
        return RecoveryProbeResult("fail", 503, None)


def _decision_for(case: RemediationRegressionCase) -> ApprovalDecision:
    return (
        ApprovalDecision.REJECT
        if case is RemediationRegressionCase.APPROVAL_REJECTED
        else ApprovalDecision.APPROVE
    )


def _record_duplicate_and_conflict(
    result: RemediationRegressionCaseResult, decision: ApprovalDecision
) -> None:
    if result.incident_id is None or result.approval is None:
        result.duplicate_same_decision = DuplicateDecisionFact(
            accepted=False, same_approval_id=False
        )
        result.conflicting_decision_rejected = False
        return
    try:
        duplicate = _record_decision(result.incident_id, decision)
        result.duplicate_same_decision = DuplicateDecisionFact(
            accepted=True,
            same_approval_id=duplicate.approval.id == result.approval.approval_id,
            resume_required=duplicate.resume_required,
        )
    except Exception:
        result.duplicate_same_decision = DuplicateDecisionFact(
            accepted=False, same_approval_id=False
        )
    opposite = (
        ApprovalDecision.REJECT
        if decision is ApprovalDecision.APPROVE
        else ApprovalDecision.APPROVE
    )
    try:
        _record_decision(result.incident_id, opposite)
    except ApprovalDecisionConflict:
        result.conflicting_decision_rejected = True
    except Exception:
        result.conflicting_decision_rejected = False
    else:
        result.conflicting_decision_rejected = False


def _collect_result_facts(result: RemediationRegressionCaseResult) -> None:
    if result.incident_id is None or result.thread_id is None:
        return
    with SessionLocal() as session:
        incident = session.get(Incident, result.incident_id)
        actions = list(
            session.scalars(select(Action).where(Action.incident_id == result.incident_id))
        )
        approvals = list(
            session.scalars(select(Approval).where(Approval.incident_id == result.incident_id))
        )
        verifications = list(
            session.scalars(
                select(Verification).where(Verification.incident_id == result.incident_id)
            )
        )
        reports = list(
            session.scalars(select(Report).where(Report.incident_id == result.incident_id))
        )
        if incident is None:
            raise RuntimeError("incident_not_persisted")
        result.action_count = len(actions)
        result.approval_count = len(approvals)
        result.verification_count = len(verifications)
        result.final_report_persisted = len(reports) == 1
        result.final_incident_status = incident.status
        state = PostgresWorkflowStateReader().get_state(result.thread_id)
        result.final_stage = AgentStage(state["current_stage"])
        terminal_reason = state["terminal_reason"]
        result.terminal_reason = (
            TerminalReason(terminal_reason) if terminal_reason is not None else None
        )
        policy_outcome = state["policy_outcome"]
        result.checkpoint_action_id = (
            policy_outcome.action_id if policy_outcome is not None else None
        )
        if len(actions) == 1:
            action = actions[0]
            result.action = RemediationActionFact(
                action_id=action.id,
                status=action.status,
                current_version=str(action.parameters.get("current_version", "")),
                target_version=str(action.parameters.get("target_version", "")),
                executed=action.executed_at is not None,
            )
        if len(approvals) == 1:
            approval = approvals[0]
            result.approval = RemediationApprovalFact(
                approval_id=approval.id,
                action_id=approval.action_id,
                status=ApprovalStatus(approval.status),
            )
        if len(verifications) == 1:
            verification = verifications[0]
            result.verification = RemediationVerificationFact(
                verification_id=verification.id,
                action_id=verification.action_id,
                status=VerificationStatus(verification.status),
            )
        result.unauthorized_execution_count = sum(
            action.executed_at is not None
            and not any(
                approval.action_id == action.id and approval.status == ApprovalStatus.APPROVED.value
                for approval in approvals
            )
            for action in actions
        )
        after = FaultLabDeploymentAdapter.from_settings().query(_deployment_input())
        result.deployment_after = RemediationDeploymentFact(
            current_version=after.current_version,
            previous_version=after.previous_version,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run deterministic remediation regression cases")
    parser.add_argument("--output", type=Path, default=DEFAULT_ARTIFACT_PATH)
    args = parser.parse_args()
    artifact = RemediationRegressionHarness().run()
    write_remediation_regression_artifact(args.output, artifact)
    print(json.dumps(artifact.assessment.model_dump(mode="json"), ensure_ascii=False))


if __name__ == "__main__":
    main()
