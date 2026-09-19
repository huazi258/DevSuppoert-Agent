"""Immutable, read-only investigation reports for the formal V2 runtime."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from devsupport_backend.agent.state import AgentState, EvidenceContext, FinalConclusion
from devsupport_backend.investigation_status import InvestigationStatus
from devsupport_backend.models import Incident, InvestigationRound, Report
from devsupport_backend.tools.schemas import CitationOutput


class V2ReportError(RuntimeError):
    """A V2 report could not be safely bound to its terminal investigation round."""


class V2InputSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str
    environment: str
    description: str
    time_range_start: datetime
    time_range_end: datetime


class V2EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    source: str
    summary: str
    reference: str | None
    citation: CitationOutput | None = None


class V2Conclusion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    root_cause: str | None
    confidence: float | None
    supporting_evidence_ids: list[str]
    contradicting_evidence_ids: list[str]
    citations: list[CitationOutput] = Field(default_factory=list)


class V2TimelineEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: str
    summary: str


class V2ReportContent(BaseModel):
    """The V2 report schema deliberately has no Action, Approval, or recovery fields."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["v2"] = "v2"
    input_summary: V2InputSummary
    conclusion: V2Conclusion | None
    hypotheses: list[dict[str, object]]
    key_evidence: list[V2EvidenceReference]
    unknowns: list[str]
    manual_suggestions: list[str]
    terminal_reason: str | None
    timeline: list[V2TimelineEntry] = Field(min_length=1)
    final_status: InvestigationStatus


class V2ReportService:
    """Persist one report snapshot for the terminal round addressed by the V2 thread."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def generate(self, state: AgentState) -> Report:
        incident = self._session.get(Incident, state["incident"].id)
        if incident is None:
            raise V2ReportError("Incident is missing")
        round_record = self._round_for_state(incident, state)
        if round_record.status not in {
            InvestigationStatus.CONCLUDED,
            InvestigationStatus.INCONCLUSIVE,
            InvestigationStatus.FAILED,
        } or incident.investigation_status is not round_record.status:
            raise V2ReportError("V2 Report requires a terminal current InvestigationRound")

        content = self._content_for(incident, round_record, state)
        existing = self._session.scalar(select(Report).where(Report.round_id == round_record.id))
        if existing is not None:
            if existing.content != content.model_dump(mode="json"):
                raise V2ReportError("Existing V2 Report conflicts with the terminal snapshot")
            return existing

        report = Report(
            incident_id=incident.id,
            round_id=round_record.id,
            version=round_record.round_number,
            content=content.model_dump(mode="json"),
            root_cause=content.conclusion.root_cause if content.conclusion else None,
        )
        self._session.add(report)
        self._session.commit()
        self._session.refresh(report)
        return report

    def _round_for_state(self, incident: Incident, state: AgentState) -> InvestigationRound:
        round_id = state.get("round_id")
        if round_id is None:
            raise V2ReportError("V2 Report requires an explicit InvestigationRound")
        round_record = self._session.get(InvestigationRound, round_id)
        if round_record is None or round_record.incident_id != incident.id:
            raise V2ReportError("V2 Report round does not belong to its Incident")
        return round_record

    def _content_for(
        self,
        incident: Incident,
        round_record: InvestigationRound,
        state: AgentState,
    ) -> V2ReportContent:
        conclusion = state["final_conclusion"]
        evidence = {item.id: item for item in state["evidence"]}
        _validate_knowledge_evidence_bindings(state, evidence)
        return V2ReportContent(
            input_summary=V2InputSummary(
                service=incident.service,
                environment=incident.environment,
                description=incident.description,
                time_range_start=incident.time_range_start,
                time_range_end=incident.time_range_end,
            ),
            conclusion=_conclusion(conclusion, evidence),
            hypotheses=[item.model_dump(mode="json") for item in state["hypotheses"]],
            key_evidence=[_evidence_reference(item) for item in evidence.values()],
            unknowns=_unknowns(state),
            manual_suggestions=_manual_suggestions(conclusion, round_record.status),
            terminal_reason=round_record.terminal_reason,
            timeline=[
                V2TimelineEntry(event="investigation_started", summary="调查轮次已启动。"),
                V2TimelineEntry(
                    event="investigation_terminal",
                    summary=f"调查以 {round_record.status.value} 结束。",
                ),
            ],
            final_status=round_record.status,
        )


def _conclusion(
    value: FinalConclusion | None, evidence: dict[UUID, EvidenceContext]
) -> V2Conclusion | None:
    if value is None:
        return None
    return V2Conclusion(
        summary=value.summary,
        root_cause=value.root_cause,
        confidence=value.confidence,
        supporting_evidence_ids=[str(item) for item in value.supporting_evidence_ids],
        contradicting_evidence_ids=[str(item) for item in value.contradicting_evidence_ids],
        citations=[
            evidence[evidence_id].citation
            for evidence_id in value.supporting_evidence_ids
            if evidence[evidence_id].citation is not None
        ],
    )


def _evidence_reference(item: EvidenceContext) -> V2EvidenceReference:
    return V2EvidenceReference(
        id=str(item.id),
        source=item.source,
        summary=item.summary,
        reference=item.reference,
        citation=item.citation,
    )


def _validate_knowledge_evidence_bindings(
    state: AgentState, evidence: dict[UUID, EvidenceContext]
) -> None:
    """Require every knowledge fact used by a hypothesis or conclusion to retain provenance."""
    referenced: set[UUID] = set()
    for hypothesis in state["hypotheses"]:
        referenced.update(hypothesis.supporting_evidence_ids)
        referenced.update(hypothesis.contradicting_evidence_ids)
    conclusion = state["final_conclusion"]
    if conclusion is not None:
        referenced.update(conclusion.supporting_evidence_ids)
        referenced.update(conclusion.contradicting_evidence_ids)
    unknown = referenced.difference(evidence)
    if unknown:
        raise V2ReportError("V2 Report references unknown Evidence")
    for evidence_id in referenced:
        item = evidence[evidence_id]
        if (
            item.source == "search_knowledge"
            and item.evidence_type == "knowledge_retrieval"
            and item.citation is None
        ):
            raise V2ReportError("knowledge Evidence requires a Citation")


def _unknowns(state: AgentState) -> list[str]:
    return [
        item.summary
        for item in state["hypotheses"]
        if item.status.value != "CONFIRMED"
    ]


def _manual_suggestions(
    conclusion: FinalConclusion | None, status: InvestigationStatus
) -> list[str]:
    if conclusion and conclusion.recommended_next_action:
        return [conclusion.recommended_next_action]
    if status is InvestigationStatus.INCONCLUSIVE:
        return ["请人工补充可验证的现象或数据后再发起新的调查轮次。"]
    if status is InvestigationStatus.FAILED:
        return ["请检查调查运行环境或数据源后再发起新的调查轮次。"]
    return []
