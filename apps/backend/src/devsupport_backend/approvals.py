"""Human Approval boundary: pause a verified Action and persist one final decision."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from devsupport_backend.agent.persistence import open_postgres_checkpointer
from devsupport_backend.agent.post_approval import (
    ControlledActionExecution,
    RecoveryVerification,
    add_post_approval_continuation,
)
from devsupport_backend.agent.runtime import WorkflowService
from devsupport_backend.agent.state import (
    ActionType,
    AgentStage,
    AgentState,
    ApprovalOutcome,
    ApprovalStatus,
    PolicyDecision,
    PolicyOutcome,
)
from devsupport_backend.database import SessionLocal
from devsupport_backend.models import Action, Approval, Incident
from devsupport_backend.schemas.approvals import ApprovalDecision
from devsupport_backend.tools.deployments import FaultLabDeploymentAdapter, FaultLabRollbackAdapter
from devsupport_backend.tools.logs import FaultLabLogsAdapter
from devsupport_backend.tools.metrics import FaultLabMetricsAdapter
from devsupport_backend.tools.recovery_probe import FaultLabRecoveryProbeAdapter

PENDING_APPROVAL = "PENDING_APPROVAL"
WAITING_APPROVAL = "WAITING_APPROVAL"


class ApprovalError(RuntimeError):
    """Base fail-closed error for invalid approval workflow state."""


class ApprovalValidationError(ApprovalError):
    """The database record and persisted workflow checkpoint do not agree."""


class ApprovalDecisionConflict(ApprovalError):
    """A final decision exists and cannot be replaced by a different decision."""


class ApprovalResumeError(ApprovalError):
    """A persisted decision could not yet advance its interrupted workflow."""


class WorkflowStateReader(Protocol):
    """Read the latest persisted AgentState for one fixed LangGraph thread."""

    def get_state(self, thread_id: str) -> AgentState:
        """Return the latest checkpoint state for the exact thread."""


class ApprovalWorkflowCoordinator(Protocol):
    """Resume one existing interrupted workflow using only its persisted thread identity."""

    def resume(self, thread_id: str) -> AgentState:
        """Wake up an interrupted workflow without starting a new investigation."""


class PostgresWorkflowStateReader:
    """Read a persisted AgentState using the official PostgreSQL checkpointer."""

    def get_state(self, thread_id: str) -> AgentState:
        with open_postgres_checkpointer() as checkpointer:
            graph = StateGraph(AgentState)
            graph.add_node("checkpoint_reader", lambda state: state)
            graph.add_edge(START, "checkpoint_reader")
            graph.add_edge("checkpoint_reader", END)
            compiled = graph.compile(checkpointer=checkpointer)
            state = compiled.get_state(WorkflowService.config_for(thread_id)).values
        if not state:
            raise ApprovalValidationError(
                "No persisted workflow checkpoint exists for this incident"
            )
        return state


class ApprovalWait(Protocol):
    """Injected workflow boundary that validates a pending Action before interrupting."""

    def enter_waiting_approval(self, state: AgentState) -> None:
        """Validate the Policy Action and persist Incident WAITING_APPROVAL."""

    def interrupt_payload(self, state: AgentState) -> dict[str, str]:
        """Return display-only data derived from the verified persisted Action."""


class ApprovalWaitService:
    """Validate the Policy-selected Action and prepare a safe interrupt payload."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def enter_waiting_approval(self, state: AgentState) -> None:
        incident, _ = self._validated_incident_and_action(state)
        incident.status = WAITING_APPROVAL
        self._session.commit()

    def interrupt_payload(self, state: AgentState) -> dict[str, str]:
        _, action = self._validated_incident_and_action(state)
        parameters = action.parameters
        required_fields = (
            "service",
            "environment",
            "current_version",
            "target_version",
            "reason",
        )
        if any(
            not isinstance(parameters.get(field), str) or not parameters[field]
            for field in required_fields
        ):
            raise ApprovalValidationError(
                "Pending Action does not contain complete display parameters"
            )
        return {
            "incident_id": str(action.incident_id),
            "action_id": str(action.id),
            "action_type": action.action_type,
            "service": parameters["service"],
            "environment": parameters["environment"],
            "current_version": parameters["current_version"],
            "target_version": parameters["target_version"],
            "reason": parameters["reason"],
        }

    def _validated_incident_and_action(self, state: AgentState) -> tuple[Incident, Action]:
        action_id = _policy_action_id(state)
        incident = self._session.get(Incident, state["incident"].id)
        action = self._session.get(Action, action_id)
        if incident is None or action is None:
            raise ApprovalValidationError("Policy Action or Incident no longer exists")
        if (
            action.incident_id != incident.id
            or action.action_type != ActionType.ROLLBACK_DEPLOYMENT.value
            or action.status != PENDING_APPROVAL
            or action.executed_at is not None
        ):
            raise ApprovalValidationError(
                "Policy Action is no longer a pending rollback for this Incident"
            )
        return incident, action


@dataclass(frozen=True)
class ApprovalDecisionResult:
    """One persisted human decision and whether its interrupted thread must be woken up."""

    approval: Approval
    resume_required: bool


class ApprovalService:
    """Persist a human decision only after revalidating the waiting checkpoint and Action."""

    def __init__(self, session: Session, workflow_state_reader: WorkflowStateReader) -> None:
        self._session = session
        self._workflow_state_reader = workflow_state_reader

    def record_decision(
        self, incident_id: UUID, decision: ApprovalDecision
    ) -> ApprovalDecisionResult:
        incident = self._session.get(Incident, incident_id)
        if incident is None:
            raise LookupError("Incident not found")
        expected_status = _status_for(decision)
        existing = self._existing_approval(incident)
        if existing is not None:
            action = self._validated_existing_binding(incident, existing)
            if existing.status == expected_status:
                return ApprovalDecisionResult(
                    approval=existing,
                    resume_required=self._existing_decision_needs_resume(
                        incident, action, existing
                    ),
                )
            raise ApprovalDecisionConflict("A different final approval decision already exists")

        if incident.status != WAITING_APPROVAL or not incident.thread_id:
            raise ApprovalValidationError("Incident is not waiting for approval")
        state = self._workflow_state_reader.get_state(incident.thread_id)
        action = self._waiting_checkpoint_action(incident, state)

        approval = Approval(
            incident_id=incident.id,
            action_id=action.id,
            status=expected_status,
        )
        self._session.add(approval)
        try:
            self._session.commit()
        except IntegrityError as error:
            self._session.rollback()
            existing = self._session.scalar(select(Approval).where(Approval.action_id == action.id))
            if existing is not None and existing.status == expected_status:
                return ApprovalDecisionResult(approval=existing, resume_required=True)
            raise ApprovalDecisionConflict("A final approval decision already exists") from error
        self._session.refresh(approval)
        return ApprovalDecisionResult(approval=approval, resume_required=True)

    def _existing_approval(self, incident: Incident) -> Approval | None:
        approvals = list(
            self._session.scalars(select(Approval).where(Approval.incident_id == incident.id))
        )
        if len(approvals) > 1:
            raise ApprovalValidationError("Incident has multiple final Approval records")
        return approvals[0] if approvals else None

    def _validated_existing_binding(self, incident: Incident, approval: Approval) -> Action:
        action = self._session.get(Action, approval.action_id)
        if action is None:
            raise ApprovalValidationError("Existing Approval Action no longer exists")
        if (
            approval.incident_id != incident.id
            or action.incident_id != incident.id
            or action.action_type != ActionType.ROLLBACK_DEPLOYMENT.value
            or approval.status not in {ApprovalStatus.APPROVED.value, ApprovalStatus.REJECTED.value}
        ):
            raise ApprovalValidationError("Existing Approval is not bound to this pending Action")
        expected_action_status = (
            ApprovalStatus.APPROVED.value
            if approval.status == ApprovalStatus.APPROVED.value
            else ApprovalStatus.REJECTED.value
        )
        allowed_statuses = {PENDING_APPROVAL, expected_action_status}
        if approval.status == ApprovalStatus.APPROVED.value and action.executed_at is not None:
            allowed_statuses.add("EXECUTED")
        if approval.status == ApprovalStatus.APPROVED.value and action.status == "FAILED":
            allowed_statuses.add("FAILED")
        if action.status not in allowed_statuses:
            raise ApprovalValidationError("Existing Approval Action is in an inconsistent status")
        return action

    def _waiting_checkpoint_action(self, incident: Incident, state: AgentState) -> Action:
        if state["incident"].id != incident.id:
            raise ApprovalValidationError("Workflow checkpoint belongs to a different Incident")
        action_id = _policy_action_id(state, required_stage=AgentStage.WAITING_APPROVAL)
        action = self._session.get(Action, action_id)
        if action is None:
            raise ApprovalValidationError("Checkpoint Policy Action no longer exists")
        if (
            action.incident_id != incident.id
            or action.action_type != ActionType.ROLLBACK_DEPLOYMENT.value
            or action.status != PENDING_APPROVAL
            or action.executed_at is not None
        ):
            raise ApprovalValidationError("Checkpoint Action is not an executable pending rollback")
        return action

    def _existing_decision_needs_resume(
        self, incident: Incident, action: Action, approval: Approval
    ) -> bool:
        if not incident.thread_id:
            raise ApprovalValidationError("Incident has no stable workflow thread")
        state = self._workflow_state_reader.get_state(incident.thread_id)
        if state["incident"].id != incident.id:
            raise ApprovalValidationError("Workflow checkpoint belongs to a different Incident")
        if state["current_stage"] == AgentStage.WAITING_APPROVAL:
            if _policy_action_id(state, required_stage=AgentStage.WAITING_APPROVAL) != action.id:
                raise ApprovalValidationError("Waiting checkpoint is bound to a different Action")
            return True
        if state["current_stage"] not in {
            AgentStage.ACTION_EXECUTION,
            AgentStage.RECOVERY_VERIFICATION,
            AgentStage.NEEDS_MANUAL_ACTION,
        }:
            raise ApprovalValidationError(
                "Workflow checkpoint cannot safely accept a duplicate decision"
            )
        outcome = state["approval_outcome"]
        if (
            not isinstance(outcome, ApprovalOutcome)
            or outcome.approval_id != approval.id
            or outcome.action_id != action.id
            or outcome.status.value != approval.status
        ):
            raise ApprovalValidationError(
                "Terminal workflow checkpoint does not match Approval record"
            )
        return False


def _policy_action_id(
    state: AgentState, *, required_stage: AgentStage | None = None
) -> UUID:
    if required_stage is not None and state.get("current_stage") != required_stage:
        raise ApprovalValidationError("Workflow checkpoint is not waiting for approval")
    outcome = state.get("policy_outcome")
    if not isinstance(outcome, PolicyOutcome):
        raise ApprovalValidationError("Workflow checkpoint has no valid Policy outcome")
    if outcome.decision is not PolicyDecision.APPROVAL_REQUIRED or outcome.action_id is None:
        raise ApprovalValidationError("Workflow checkpoint does not require approval for an Action")
    return outcome.action_id


def _status_for(decision: ApprovalDecision) -> str:
    return (
        ApprovalStatus.APPROVED.value
        if decision is ApprovalDecision.APPROVE
        else ApprovalStatus.REJECTED.value
    )


def approval_wait_node(state: AgentState, approval_wait: ApprovalWait) -> AgentState:
    """Persist WAITING_APPROVAL before the next node creates the LangGraph interrupt."""
    _policy_action_id(state, required_stage=AgentStage.POLICY_GATE)
    approval_wait.enter_waiting_approval(state)
    return {**state, "current_stage": AgentStage.WAITING_APPROVAL}


def approval_interrupt_node(state: AgentState, approval_wait: ApprovalWait) -> AgentState:
    """Pause, then treat any resume value only as a wake-up signal for database revalidation."""
    if state["current_stage"] != AgentStage.WAITING_APPROVAL:
        raise ApprovalValidationError("Workflow did not enter WAITING_APPROVAL before interrupt")
    interrupt(approval_wait.interrupt_payload(state))
    return state


class ApprovalDecisionService:
    """Re-read the persisted Approval and move only to the next non-executing workflow stage."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def resolve(self, state: AgentState) -> ApprovalOutcome:
        if state["current_stage"] != AgentStage.WAITING_APPROVAL:
            raise ApprovalValidationError(
                "Approval decision requires a WAITING_APPROVAL checkpoint"
            )
        incident = self._session.get(Incident, state["incident"].id)
        action_id = _policy_action_id(state, required_stage=AgentStage.WAITING_APPROVAL)
        action = self._session.get(Action, action_id)
        approval = self._session.scalar(select(Approval).where(Approval.action_id == action_id))
        if incident is None or action is None or approval is None:
            raise ApprovalValidationError("Approval decision record is missing from PostgreSQL")
        if (
            action.incident_id != incident.id
            or action.action_type != ActionType.ROLLBACK_DEPLOYMENT.value
            or action.executed_at is not None
            or approval.action_id != action.id
            or approval.incident_id != incident.id
            or approval.status not in {ApprovalStatus.APPROVED.value, ApprovalStatus.REJECTED.value}
        ):
            raise ApprovalValidationError(
                "Approval decision records are inconsistent with the checkpoint"
            )

        outcome = ApprovalOutcome(
            approval_id=approval.id,
            action_id=action.id,
            status=ApprovalStatus(approval.status),
        )
        if action.status not in {PENDING_APPROVAL, outcome.status.value}:
            raise ApprovalValidationError(
                "Action status is inconsistent with the Approval decision"
            )
        if outcome.status is ApprovalStatus.APPROVED:
            action.status = ApprovalStatus.APPROVED.value
            incident.status = "REMEDIATING"
        else:
            action.status = ApprovalStatus.REJECTED.value
            incident.status = "NEEDS_MANUAL_ACTION"
        self._session.commit()
        return outcome


def approval_decision_node(
    state: AgentState, approval_decision: ApprovalDecisionService
) -> AgentState:
    """Advance a resumed workflow from PostgreSQL facts only; never execute remediation here."""
    outcome = approval_decision.resolve(state)
    return {
        **state,
        "approval_outcome": outcome,
        "current_stage": (
            AgentStage.ACTION_EXECUTION
            if outcome.status is ApprovalStatus.APPROVED
            else AgentStage.NEEDS_MANUAL_ACTION
        ),
    }


def build_approval_resume_graph(
    *,
    approval_wait: ApprovalWait,
    approval_decision: ApprovalDecisionService,
    action_execution: ControlledActionExecution | None = None,
    recovery_verification: RecoveryVerification | None = None,
    checkpointer: BaseCheckpointSaver,
):
    """Compile the persisted continuation for the original approval-interrupted workflow."""
    graph = StateGraph(AgentState)
    graph.add_node(
        "approval_interrupt",
        lambda state: approval_interrupt_node(state, approval_wait),
    )
    graph.add_node(
        "approval_decision",
        lambda state: approval_decision_node(state, approval_decision),
    )
    graph.add_edge(START, "approval_interrupt")
    graph.add_edge("approval_interrupt", "approval_decision")
    if action_execution is None:
        graph.add_edge("approval_decision", END)
    else:
        add_post_approval_continuation(
            graph,
            action_execution=action_execution,
            recovery_verification=recovery_verification,
        )
    return graph.compile(checkpointer=checkpointer)


class PostgresApprovalWorkflowCoordinator:
    """Reopen the official PostgreSQL graph continuation for exactly one existing thread."""

    def resume(self, thread_id: str) -> AgentState:
        try:
            with SessionLocal() as session, open_postgres_checkpointer() as checkpointer:
                graph = build_approval_resume_graph(
                    approval_wait=ApprovalWaitService(session),
                    approval_decision=ApprovalDecisionService(session),
                    action_execution=_action_execution_service(session),
                    recovery_verification=_recovery_verification_service(session),
                    checkpointer=checkpointer,
                )
                return WorkflowService(graph).resume(
                    thread_id, {"event": "approval_recorded"}
                )
        except ApprovalError:
            raise
        except Exception as error:
            raise ApprovalResumeError(
                "Workflow resume did not complete; retry the same decision"
            ) from error


def _action_execution_service(session: Session):
    """Construct the one production side-effect boundary for a resumed Action."""
    from devsupport_backend.action_execution import ActionExecutionService

    return ActionExecutionService(
        session,
        FaultLabDeploymentAdapter.from_settings(),
        FaultLabRollbackAdapter.from_settings(),
    )


def _recovery_verification_service(session: Session):
    from devsupport_backend.recovery_verification import RecoveryVerificationService

    return RecoveryVerificationService(
        session,
        FaultLabDeploymentAdapter.from_settings(),
        FaultLabMetricsAdapter.from_settings(),
        FaultLabLogsAdapter.from_settings(),
        FaultLabRecoveryProbeAdapter.from_settings(),
    )
