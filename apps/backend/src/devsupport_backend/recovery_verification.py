"""Deterministic, post-rollback recovery verification; never a Planner Tool."""
# ruff: noqa: E501

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from devsupport_backend.action_execution import ActionExecutionParameters
from devsupport_backend.agent.state import (
    ActionExecutionOutcome,
    ActionType,
    AgentStage,
    AgentState,
    ApprovalOutcome,
    ApprovalStatus,
    PolicyDecision,
    PolicyOutcome,
    VerificationOutcome,
    VerificationStatus,
)
from devsupport_backend.models import Action, Approval, Incident, Verification
from devsupport_backend.tools.deployments import DeploymentAdapterError, FaultLabDeploymentAdapter
from devsupport_backend.tools.logs import FaultLabLogsAdapter, LogsAdapterError
from devsupport_backend.tools.metrics import FaultLabMetricsAdapter, MetricsAdapterError
from devsupport_backend.tools.recovery_probe import FaultLabRecoveryProbeAdapter
from devsupport_backend.tools.schemas import (
    GetDeploymentHistoryInput,
    QueryLogsInput,
    QueryMetricsInput,
)


class RecoveryVerificationService:
    """Persist exactly one final verification from fixed, freshly observed evidence."""

    def __init__(
        self,
        session: Session,
        deployment_adapter: FaultLabDeploymentAdapter,
        metrics_adapter: FaultLabMetricsAdapter,
        logs_adapter: FaultLabLogsAdapter,
        probe_adapter: FaultLabRecoveryProbeAdapter,
    ) -> None:
        self._session = session
        self._deployment_adapter = deployment_adapter
        self._metrics_adapter = metrics_adapter
        self._logs_adapter = logs_adapter
        self._probe_adapter = probe_adapter

    def verify(self, state: AgentState) -> VerificationOutcome:
        policy = state.get("policy_outcome")
        if isinstance(policy, PolicyOutcome) and policy.action_id is not None:
            existing = self._session.scalar(
                select(Verification).where(Verification.action_id == policy.action_id)
            )
            action = self._session.get(Action, policy.action_id)
            if (
                existing is not None
                and action is not None
                and existing.incident_id == state["incident"].id
                and action.incident_id == existing.incident_id
                and action.status == "EXECUTED"
                and action.executed_at is not None
                and existing.status in set(VerificationStatus)
                and self._complete_audit_chain_matches(state, action, existing)
            ):
                return VerificationOutcome(
                    verification_id=existing.id,
                    action_id=action.id,
                    status=VerificationStatus(existing.status),
                    summary=existing.summary,
                )
        try:
            incident, action, approval, execution, parameters = self._validated_records(state)
        except (RecoveryVerificationError, ValueError):
            return self._binding_inconclusive(state)
        started_at = datetime.now(UTC)
        details: dict[str, object] = {
            "verification_started_at": started_at.isoformat(),
            "deployment": {"expected_target": parameters.target_version},
        }
        try:
            deployment = self._deployment_adapter.query(
                GetDeploymentHistoryInput(
                    service=parameters.service, environment=parameters.environment
                )
            )
            details["deployment"] = {
                "expected_target": parameters.target_version,
                "current_version": deployment.current_version,
                "previous_version": deployment.previous_version,
                "passed": deployment.current_version == parameters.target_version
                and deployment.previous_version == parameters.current_version,
            }
        except DeploymentAdapterError:
            return self._persist(
                incident,
                action,
                VerificationStatus.INCONCLUSIVE,
                details,
                "Deployment state was unavailable.",
            )
        if not details["deployment"]["passed"]:  # type: ignore[index]
            return self._persist(
                incident,
                action,
                VerificationStatus.FAIL,
                details,
                "Deployment does not match the approved rollback target.",
            )

        try:
            before = self._metrics_adapter.query(
                QueryMetricsInput(service=parameters.service, environment="local")
            )
        except MetricsAdapterError:
            return self._persist(
                incident,
                action,
                VerificationStatus.INCONCLUSIVE,
                details,
                "Health or metrics were unavailable.",
            )
        details["health"] = {"status": before.health_status, "passed": before.health_status == "ok"}
        details["metrics_before"] = _metrics(before)
        if before.health_status != "ok":
            return self._persist(
                incident, action, VerificationStatus.FAIL, details, "Service health is not ok."
            )

        probe = self._probe_adapter.probe()
        details["core_request"] = {
            "http_status": probe.http_status,
            "response_status": probe.response_status,
            "passed": probe.outcome == "pass",
        }
        if probe.outcome == "inconclusive":
            return self._persist(
                incident,
                action,
                VerificationStatus.INCONCLUSIVE,
                details,
                "Recovery probe was unavailable.",
            )
        if probe.outcome != "pass":
            return self._persist(
                incident, action, VerificationStatus.FAIL, details, "Recovery core request failed."
            )
        try:
            after = self._metrics_adapter.query(
                QueryMetricsInput(service=parameters.service, environment="local")
            )
        except MetricsAdapterError:
            return self._persist(
                incident,
                action,
                VerificationStatus.INCONCLUSIVE,
                details,
                "Post-probe metrics were unavailable.",
            )
        delta = {
            "request_count": after.request_count - before.request_count,
            "success_count": after.success_count - before.success_count,
            "error_count": after.error_count - before.error_count,
        }
        metric_passed = delta == {"request_count": 1, "success_count": 1, "error_count": 0}
        details["metrics_after"] = _metrics(after)
        details["metrics_delta"] = {**delta, "passed": metric_passed}
        if delta["error_count"] > 0 or (
            delta["request_count"] == 1 and delta["success_count"] == 0
        ):
            return self._persist(
                incident,
                action,
                VerificationStatus.FAIL,
                details,
                "Recovery probe produced an error metric signal.",
            )
        if not metric_passed:
            return self._persist(
                incident,
                action,
                VerificationStatus.INCONCLUSIVE,
                details,
                "Recovery metric delta was not uniquely attributable.",
            )

        completed_at = datetime.now(UTC)
        try:
            logs = self._logs_adapter.query(
                QueryLogsInput(
                    service=parameters.service,
                    environment="local",
                    time_range_start=action.executed_at,
                    time_range_end=completed_at,
                    level="error",
                    limit=100,
                )
            )
        except LogsAdapterError:
            return self._persist(
                incident,
                action,
                VerificationStatus.INCONCLUSIVE,
                details,
                "Post-action error logs were unavailable.",
            )
        details["new_error_logs"] = {
            "count": logs.match_count,
            "passed": logs.match_count == 0,
            "window_start": action.executed_at.isoformat(),
            "window_end": completed_at.isoformat(),
        }
        details["verification_completed_at"] = completed_at.isoformat()
        if logs.match_count:
            return self._persist(
                incident,
                action,
                VerificationStatus.FAIL,
                details,
                "New error logs appeared after rollback.",
            )
        return self._persist(
            incident,
            action,
            VerificationStatus.PASS,
            details,
            "Approved rollback recovery verified by deployment, health, core request, metrics delta, and post-action logs.",
        )

    def _validated_records(self, state: AgentState):
        if state["current_stage"] is not AgentStage.RECOVERY_VERIFICATION:
            raise RecoveryVerificationError("Recovery verification requires its dedicated stage")
        policy, approval_outcome, execution = (
            state["policy_outcome"],
            state["approval_outcome"],
            state["execution_outcome"],
        )
        if (
            not isinstance(policy, PolicyOutcome)
            or policy.decision is not PolicyDecision.APPROVAL_REQUIRED
            or policy.action_id is None
            or not isinstance(approval_outcome, ApprovalOutcome)
            or not isinstance(execution, ActionExecutionOutcome)
        ):
            raise RecoveryVerificationError(
                "Recovery verification state has no exact Action binding"
            )
        incident = self._session.get(Incident, state["incident"].id)
        action = self._session.get(Action, policy.action_id)
        approval = self._session.get(Approval, approval_outcome.approval_id)
        if (
            incident is None
            or action is None
            or approval is None
            or incident.status != "VERIFYING"
            or action.id != approval_outcome.action_id
            or action.id != execution.action_id
            or approval.id != execution.approval_id
            or approval.action_id != action.id
            or approval.incident_id != incident.id
            or approval.status != ApprovalStatus.APPROVED.value
            or action.incident_id != incident.id
            or action.action_type != ActionType.ROLLBACK_DEPLOYMENT.value
            or action.status != "EXECUTED"
            or action.executed_at is None
            or execution.status.value != "success"
            or execution.target_version is None
            or execution.service is None
            or execution.environment != "local"
        ):
            raise RecoveryVerificationError("Recovery verification records are inconsistent")
        parameters = ActionExecutionParameters.model_validate(action.parameters)
        if (
            execution.target_version != parameters.target_version
            or execution.service != parameters.service
            or parameters.environment != "local"
        ):
            raise RecoveryVerificationError(
                "Executed Action target does not match its persisted parameters"
            )
        return incident, action, approval, execution, parameters

    def _persist(
        self,
        incident: Incident,
        action: Action,
        status: VerificationStatus,
        details: dict[str, object],
        summary: str,
    ) -> VerificationOutcome:
        details.setdefault("verification_completed_at", datetime.now(UTC).isoformat())
        verification = Verification(
            incident_id=incident.id,
            action_id=action.id,
            status=status.value,
            summary=summary,
            details=details,
        )
        self._session.add(verification)
        incident.status = "RESOLVED" if status is VerificationStatus.PASS else "NEEDS_MANUAL_ACTION"
        self._session.commit()
        self._session.refresh(verification)
        return VerificationOutcome(
            verification_id=verification.id, action_id=action.id, status=status, summary=summary
        )

    def _binding_inconclusive(self, state: AgentState) -> VerificationOutcome:
        """Fail closed without inventing an Action when only the Incident is trustworthy."""
        incident = self._session.get(Incident, state["incident"].id)
        policy = state.get("policy_outcome")
        action = (
            self._session.get(Action, policy.action_id)
            if isinstance(policy, PolicyOutcome) and policy.action_id is not None
            else None
        )
        if incident is None:
            return VerificationOutcome(
                status=VerificationStatus.INCONCLUSIVE,
                summary="Recovery verification binding could not be established.",
            )
        if (
            action is not None
            and action.incident_id == incident.id
            and action.action_type == ActionType.ROLLBACK_DEPLOYMENT.value
            and action.status == "EXECUTED"
            and action.executed_at is not None
        ):
            now = datetime.now(UTC).isoformat()
            return self._persist(
                incident,
                action,
                VerificationStatus.INCONCLUSIVE,
                {"verification_started_at": now, "verification_completed_at": now},
                "Recovery verification binding could not be established.",
            )
        incident.status = "NEEDS_MANUAL_ACTION"
        self._session.commit()
        return VerificationOutcome(
            status=VerificationStatus.INCONCLUSIVE,
            summary="Recovery verification binding could not be established.",
        )

    def _complete_audit_chain_matches(
        self, state: AgentState, action: Action, verification: Verification
    ) -> bool:
        incident = self._session.get(Incident, state["incident"].id)
        approval_outcome, execution = state.get("approval_outcome"), state.get("execution_outcome")
        if (
            incident is None
            or verification.incident_id != incident.id
            or verification.action_id != action.id
            or action.incident_id != incident.id
            or action.action_type != ActionType.ROLLBACK_DEPLOYMENT.value
            or action.status != "EXECUTED"
            or action.executed_at is None
            or not isinstance(approval_outcome, ApprovalOutcome)
            or approval_outcome.action_id != action.id
            or approval_outcome.status is not ApprovalStatus.APPROVED
            or not isinstance(execution, ActionExecutionOutcome)
            or execution.status.value != "success"
            or execution.action_id != action.id
            or execution.approval_id != approval_outcome.approval_id
        ):
            return False
        approval = self._session.get(Approval, approval_outcome.approval_id)
        if (
            approval is None
            or approval.incident_id != incident.id
            or approval.action_id != action.id
            or approval.status != ApprovalStatus.APPROVED.value
        ):
            return False
        try:
            parameters = ActionExecutionParameters.model_validate(action.parameters)
        except ValueError:
            return False
        return (
            execution.service == parameters.service
            and execution.environment == parameters.environment
            and execution.target_version == parameters.target_version
        )


class RecoveryVerificationError(RuntimeError):
    pass


def _metrics(metrics) -> dict[str, int]:
    return {
        "request_count": metrics.request_count,
        "success_count": metrics.success_count,
        "error_count": metrics.error_count,
    }


def recovery_verification_node(
    state: AgentState, service: RecoveryVerificationService
) -> AgentState:
    outcome = service.verify(state)
    return {
        **state,
        "verification_outcome": outcome,
        "current_stage": AgentStage.RESOLVED
        if outcome.status is VerificationStatus.PASS
        else AgentStage.NEEDS_MANUAL_ACTION,
    }
