"""Deterministic persistence boundary for non-remediation terminal workflow paths."""

from sqlalchemy.orm import Session

from devsupport_backend.agent.state import AgentStage, AgentState
from devsupport_backend.models import Incident


class ManualTerminalizationError(RuntimeError):
    """The checkpoint cannot safely terminalize its authoritative Incident."""


class PostgresManualTerminalizer:
    """Persist NEEDS_MANUAL_ACTION before a terminal report is generated."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def mark_needs_manual_action(self, state: AgentState) -> AgentState:
        context = state["incident"]
        incident = self._session.get(Incident, context.id)
        if incident is None:
            raise ManualTerminalizationError("Incident is missing")
        if (
            incident.service != context.service
            or incident.environment != context.environment
            or incident.description != context.description
            or incident.time_range_start != context.time_range_start
            or incident.time_range_end != context.time_range_end
        ):
            raise ManualTerminalizationError("Checkpoint Incident binding mismatch")
        if incident.status == "RESOLVED":
            raise ManualTerminalizationError("Resolved Incident cannot be terminalized manually")
        incident.status = "NEEDS_MANUAL_ACTION"
        self._session.commit()
        return {**state, "current_stage": AgentStage.NEEDS_MANUAL_ACTION}
