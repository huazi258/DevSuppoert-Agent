"""The only V0 side-effect boundary: an approved, current local rollback."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from devsupport_backend.agent.state import (
    ActionExecutionOutcome,
    ActionType,
    AgentStage,
    AgentState,
    ApprovalStatus,
    PolicyDecision,
    PolicyOutcome,
)
from devsupport_backend.models import Action, Approval, Incident
from devsupport_backend.tools.deployments import (
    SUPPORTED_ENVIRONMENT,
    DeploymentAdapterError,
    FaultLabDeploymentAdapter,
    FaultLabRollbackAdapter,
)
from devsupport_backend.tools.rollback import rollback_deployment
from devsupport_backend.tools.schemas import (
    GetDeploymentHistoryInput,
    RollbackDeploymentInput,
    ToolStatus,
)


class ActionExecutionParameters(BaseModel):
    """Only Policy Gate-persisted facts may form a controlled rollback request."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    service: str = Field(min_length=1, max_length=100)
    environment: str = Field(min_length=1, max_length=50)
    current_version: str = Field(min_length=1, max_length=100)
    target_version: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2_000)


class ActionExecutionService:
    """Revalidate database and live deployment facts before one local rollback."""

    def __init__(
        self,
        session: Session,
        deployment_adapter: FaultLabDeploymentAdapter,
        rollback_adapter: FaultLabRollbackAdapter,
    ) -> None:
        self._session = session
        self._deployment_adapter = deployment_adapter
        self._rollback_adapter = rollback_adapter

    def execute(self, state: AgentState) -> ActionExecutionOutcome:
        """Fail closed unless the exact approved Action remains safe to execute."""
        try:
            incident, action, approval, parameters = self._validated_records(state)
            live = self._deployment_adapter.query(
                GetDeploymentHistoryInput(
                    service=parameters.service, environment=parameters.environment
                )
            )
            if (
                live.current_version == parameters.target_version
                and live.previous_version == parameters.current_version
            ):
                self._mark_executed(action, incident)
                return self._outcome(action, approval, parameters, executed=False)
            if (
                live.current_version != parameters.current_version
                or live.previous_version != parameters.target_version
            ):
                raise ActionExecutionError("Live deployment no longer matches the approved Action")
            result = rollback_deployment(
                RollbackDeploymentInput(
                    service=parameters.service,
                    environment=parameters.environment,
                    target_version=parameters.target_version,
                    reason=parameters.reason,
                    approval_id=approval.id,
                ),
                self._rollback_adapter,
            )
            if (
                result.status is not ToolStatus.SUCCESS
                or result.service != parameters.service
                or result.target_version != parameters.target_version
            ):
                raise ActionExecutionError("Rollback result does not match the approved Action")
            self._mark_executed(action, incident)
            return self._outcome(action, approval, parameters, executed=result.executed)
        except (ActionExecutionError, DeploymentAdapterError, ValueError):
            return self._fail_closed(state)

    def _validated_records(
        self, state: AgentState
    ) -> tuple[Incident, Action, Approval, ActionExecutionParameters]:
        if state["current_stage"] != AgentStage.ACTION_EXECUTION:
            raise ActionExecutionError("Action execution requires an ACTION_EXECUTION checkpoint")
        policy = state["policy_outcome"]
        approval_outcome = state["approval_outcome"]
        if (
            not isinstance(policy, PolicyOutcome)
            or policy.decision is not PolicyDecision.APPROVAL_REQUIRED
            or policy.action_id is None
            or approval_outcome is None
            or approval_outcome.action_id != policy.action_id
            or approval_outcome.status is not ApprovalStatus.APPROVED
        ):
            raise ActionExecutionError("Checkpoint is not bound to an approved Policy Action")
        incident = self._session.get(Incident, state["incident"].id)
        action = self._session.get(Action, policy.action_id)
        approval = self._session.get(Approval, approval_outcome.approval_id)
        if incident is None or action is None or approval is None:
            raise ActionExecutionError("Approved Action records are missing")
        if (
            incident.status != "REMEDIATING"
            or action.incident_id != incident.id
            or action.action_type != ActionType.ROLLBACK_DEPLOYMENT.value
            or action.status != ApprovalStatus.APPROVED.value
            or action.executed_at is not None
            or approval.incident_id != incident.id
            or approval.action_id != action.id
            or approval.status != ApprovalStatus.APPROVED.value
        ):
            raise ActionExecutionError("Approved Action records fail execution revalidation")
        parameters = ActionExecutionParameters.model_validate(action.parameters)
        if (
            parameters.service != incident.service
            or parameters.environment != incident.environment
            or parameters.environment != SUPPORTED_ENVIRONMENT
            or parameters.service != "order-service"
            or parameters.current_version == parameters.target_version
        ):
            raise ActionExecutionError("Approved Action parameters are not executable locally")
        return incident, action, approval, parameters

    def _mark_executed(self, action: Action, incident: Incident) -> None:
        action.status = "EXECUTED"
        action.executed_at = datetime.now(UTC)
        incident.status = "VERIFYING"
        self._session.commit()

    def _outcome(
        self,
        action: Action,
        approval: Approval,
        parameters: ActionExecutionParameters,
        *,
        executed: bool,
    ) -> ActionExecutionOutcome:
        return ActionExecutionOutcome(
            action_id=action.id,
            approval_id=approval.id,
            status=ToolStatus.SUCCESS,
            service=parameters.service,
            environment=parameters.environment,
            target_version=parameters.target_version,
            executed=executed,
        )

    def _fail_closed(self, state: AgentState) -> ActionExecutionOutcome:
        policy = state.get("policy_outcome")
        approval_outcome = state.get("approval_outcome")
        action_id = policy.action_id if isinstance(policy, PolicyOutcome) else None
        approval_id = approval_outcome.approval_id if approval_outcome is not None else None
        action = self._session.get(Action, action_id) if action_id is not None else None
        approval = self._session.get(Approval, approval_id) if approval_id is not None else None
        incident = self._session.get(Incident, state["incident"].id)
        if action is not None and action.executed_at is None:
            action.status = "FAILED"
        if incident is not None:
            incident.status = "NEEDS_MANUAL_ACTION"
        self._session.commit()
        try:
            parameters = (
                ActionExecutionParameters.model_validate(action.parameters) if action else None
            )
        except ValueError:
            parameters = None
        valid_action = (
            action if action is not None and action.incident_id == state["incident"].id else None
        )
        valid_approval = (
            approval
            if valid_action is not None
            and approval is not None
            and approval.incident_id == valid_action.incident_id
            and approval.action_id == valid_action.id
            else None
        )
        return ActionExecutionOutcome(
            action_id=valid_action.id if valid_action is not None else None,
            approval_id=valid_approval.id if valid_approval is not None else None,
            status=ToolStatus.FAILURE,
            service=parameters.service if parameters else None,
            environment=parameters.environment if parameters else None,
            target_version=parameters.target_version if parameters else None,
            executed=False,
        )


class ActionExecutionError(RuntimeError):
    """Execution prerequisites, deployment state, or response failed closed."""


def controlled_action_execution_node(
    state: AgentState, action_execution: ActionExecutionService
) -> AgentState:
    """Execute at most the exact database-approved rollback and stop for verification."""
    outcome = action_execution.execute(state)
    return {
        **state,
        "execution_outcome": outcome,
        "current_stage": (
            AgentStage.RECOVERY_VERIFICATION
            if outcome.status is ToolStatus.SUCCESS
            else AgentStage.NEEDS_MANUAL_ACTION
        ),
    }
