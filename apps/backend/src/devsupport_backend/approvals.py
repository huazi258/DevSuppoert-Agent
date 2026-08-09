"""Human Approval boundary: pause a verified Action and persist one final decision."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from devsupport_backend.agent.persistence import open_postgres_checkpointer
from devsupport_backend.agent.runtime import WorkflowService
from devsupport_backend.agent.state import (
    ActionType,
    AgentStage,
    AgentState,
    PolicyDecision,
    PolicyOutcome,
)
from devsupport_backend.models import Action, Approval, Incident
from devsupport_backend.schemas.approvals import ApprovalDecision, ApprovalStatus

PENDING_APPROVAL = "PENDING_APPROVAL"
WAITING_APPROVAL = "WAITING_APPROVAL"


class ApprovalError(RuntimeError):
    """Base fail-closed error for invalid approval workflow state."""


class ApprovalValidationError(ApprovalError):
    """The database record and persisted workflow checkpoint do not agree."""


class ApprovalDecisionConflict(ApprovalError):
    """A final decision exists and cannot be replaced by a different decision."""


class WorkflowStateReader(Protocol):
    """Read the latest persisted AgentState for one fixed LangGraph thread."""

    def get_state(self, thread_id: str) -> AgentState:
        """Return the latest checkpoint state for the exact thread."""


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


class ApprovalService:
    """Persist a human decision only after revalidating the waiting checkpoint and Action."""

    def __init__(self, session: Session, workflow_state_reader: WorkflowStateReader) -> None:
        self._session = session
        self._workflow_state_reader = workflow_state_reader

    def record_decision(self, incident_id: UUID, decision: ApprovalDecision) -> Approval:
        incident = self._session.get(Incident, incident_id)
        if incident is None:
            raise LookupError("Incident not found")
        if incident.status != WAITING_APPROVAL or not incident.thread_id:
            raise ApprovalValidationError("Incident is not waiting for approval")

        state = self._workflow_state_reader.get_state(incident.thread_id)
        action_id = _policy_action_id(state, required_stage=AgentStage.WAITING_APPROVAL)
        action = self._session.get(Action, action_id)
        if action is None or action.id != action_id:
            raise ApprovalValidationError("Checkpoint Policy Action no longer exists")
        if (
            action.incident_id != incident.id
            or action.action_type != ActionType.ROLLBACK_DEPLOYMENT.value
            or action.status != PENDING_APPROVAL
            or action.executed_at is not None
        ):
            raise ApprovalValidationError("Checkpoint Action is not an executable pending rollback")

        existing = self._session.scalar(select(Approval).where(Approval.action_id == action.id))
        expected_status = _status_for(decision)
        if existing is not None:
            if existing.status == expected_status:
                return existing
            raise ApprovalDecisionConflict("A different final approval decision already exists")

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
                return existing
            raise ApprovalDecisionConflict("A final approval decision already exists") from error
        self._session.refresh(approval)
        return approval


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
    """Pause only after WAITING_APPROVAL is checkpointed by the preceding node."""
    if state["current_stage"] != AgentStage.WAITING_APPROVAL:
        raise ApprovalValidationError("Workflow did not enter WAITING_APPROVAL before interrupt")
    interrupt(approval_wait.interrupt_payload(state))
    return state
