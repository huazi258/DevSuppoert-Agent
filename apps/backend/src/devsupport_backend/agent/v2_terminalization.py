"""V2-only lifecycle and report boundary for a read-only investigation graph."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from devsupport_backend.agent.state import AgentStage, AgentState, ReportOutcome
from devsupport_backend.investigation_lifecycle import InvestigationLifecycleService
from devsupport_backend.investigation_status import InvestigationStatus
from devsupport_backend.models import Incident, InvestigationRound
from devsupport_backend.v2_report import V2ReportService


class V2TerminalizationError(RuntimeError):
    """The active V2 thread cannot be bound to one authoritative round."""


class V2Terminalizer:
    """Use the M1.2 lifecycle service before creating the immutable V2 report."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def terminalize(self, state: AgentState, status: InvestigationStatus) -> AgentState:
        incident = self._session.get(Incident, state["incident"].id)
        if incident is None:
            raise V2TerminalizationError("Incident is missing")
        round_record = self._session.scalar(
            select(InvestigationRound).where(
                InvestigationRound.incident_id == incident.id,
                InvestigationRound.thread_id == incident.thread_id,
            )
        )
        if round_record is None:
            raise V2TerminalizationError("Incident has no InvestigationRound for its V2 thread")
        terminal_reason = state.get("terminal_reason")
        InvestigationLifecycleService(self._session).terminalize(
            round_record.id,
            status,
            terminal_reason=terminal_reason.value if terminal_reason is not None else None,
        )
        report = V2ReportService(self._session).generate(state)
        return {
            **state,
            "current_stage": AgentStage.CONCLUSION,
            "report_outcome": ReportOutcome(
                report_id=report.id,
                incident_id=incident.id,
                final_status=status.value,
            ),
        }
