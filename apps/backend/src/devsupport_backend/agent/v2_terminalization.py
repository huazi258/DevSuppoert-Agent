"""V2-only lifecycle and report boundary for a read-only investigation graph."""

from __future__ import annotations

from sqlalchemy.orm import Session

from devsupport_backend.agent.state import AgentStage, AgentState, ReportOutcome
from devsupport_backend.investigation_lifecycle import InvestigationLifecycleService
from devsupport_backend.investigation_status import InvestigationStatus
from devsupport_backend.models import Evidence, Hypothesis, Incident, InvestigationRound, ToolCall
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
        round_id = state.get("round_id")
        if round_id is None:
            raise V2TerminalizationError("V2 state requires an explicit InvestigationRound")
        round_record = self._session.get(InvestigationRound, round_id)
        if round_record is None or round_record.incident_id != incident.id:
            raise V2TerminalizationError("V2 state round does not belong to its Incident")
        self._persist_round_records(state, round_record)
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

    def _persist_round_records(
        self, state: AgentState, round_record: InvestigationRound
    ) -> None:
        """Persist V2 runtime facts with explicit ownership before terminalizing the round."""
        hypotheses: dict[object, Hypothesis] = {}
        evidence_hypotheses: dict[object, Hypothesis] = {}
        for item in state["hypotheses"]:
            hypothesis = Hypothesis(
                id=item.id,
                incident_id=round_record.incident_id,
                round_id=round_record.id,
                summary=item.summary,
                status=item.status.value,
                confidence=item.confidence,
                details={
                    "supporting_evidence_ids": [
                        str(value) for value in item.supporting_evidence_ids
                    ],
                    "contradicting_evidence_ids": [
                        str(value) for value in item.contradicting_evidence_ids
                    ],
                    "next_check": item.next_check,
                },
            )
            hypotheses[item.id] = hypothesis
            for evidence_id in item.supporting_evidence_ids + item.contradicting_evidence_ids:
                evidence_hypotheses.setdefault(evidence_id, hypothesis)
        self._session.add_all(hypotheses.values())
        for item in state["evidence"]:
            self._session.add(
                Evidence(
                    id=item.id,
                    incident_id=round_record.incident_id,
                    round_id=round_record.id,
                    hypothesis=evidence_hypotheses.get(item.id),
                    evidence_type=item.evidence_type,
                    source=item.source,
                    content=item.summary,
                    data={**item.data, "reference": item.reference},
                )
            )
        for item in state["tool_history"]:
            self._session.add(
                ToolCall(
                    incident_id=round_record.incident_id,
                    round_id=round_record.id,
                    tool_name=item.tool_name.value,
                    status=item.status.value,
                    input_data=item.tool_arguments,
                    result={"evidence_ids": [str(value) for value in item.evidence_ids]}
                    if item.status.value == "SUCCESS"
                    else None,
                    error=item.error.message if item.error is not None else None,
                    duration_ms=item.duration_ms,
                )
            )
        self._session.flush()
