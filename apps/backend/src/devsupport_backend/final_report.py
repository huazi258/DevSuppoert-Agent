"""Deterministic, typed final Incident report projection; no new investigation facts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy import select
from sqlalchemy.orm import Session

from devsupport_backend.action_execution import ActionExecutionParameters
from devsupport_backend.agent.state import (
    ActionExecutionOutcome,
    AgentState,
    ApprovalOutcome,
    ReportOutcome,
    VerificationOutcome,
)
from devsupport_backend.models import Action, Approval, Incident, Report, Verification


class StrictReportModel(BaseModel):
    """Reject unmodelled report facts instead of silently stringifying them."""

    model_config = ConfigDict(extra="forbid")


class IncidentReportSection(StrictReportModel):
    incident_id: UUID
    service: str
    environment: str
    description: str
    time_range_start: datetime
    time_range_end: datetime
    final_status: Literal["RESOLVED", "NEEDS_MANUAL_ACTION"]
    created_at: datetime


class RootCauseReportSection(StrictReportModel):
    summary: str
    root_cause: str | None
    confidence: float | None
    supporting_evidence_ids: list[UUID]
    contradicting_evidence_ids: list[UUID]
    recommended_next_action: str | None


class HypothesisReportItem(StrictReportModel):
    id: UUID
    summary: str
    status: str
    confidence: float | None
    supporting_evidence_ids: list[UUID]
    contradicting_evidence_ids: list[UUID]
    next_check: str | None


class EvidenceReportItem(StrictReportModel):
    id: UUID
    evidence_type: str
    source: str
    summary: str
    reference: str | None


class RecommendedActionReportSection(StrictReportModel):
    action_type: str
    summary: str
    reason: str
    risk: str
    supporting_evidence_ids: list[UUID]


class ActionReportSection(StrictReportModel):
    action_id: UUID
    action_type: str
    status: str
    parameters: dict[str, JsonValue]
    created_at: datetime
    executed_at: datetime | None


class ApprovalReportSection(StrictReportModel):
    approval_id: UUID
    action_id: UUID
    status: str
    created_at: datetime
    updated_at: datetime


class ExecutionReportSection(StrictReportModel):
    action_id: UUID | None
    approval_id: UUID | None
    status: str
    service: str | None
    environment: str | None
    target_version: str | None
    executed: bool


class VerificationReportSection(StrictReportModel):
    verification_id: UUID
    action_id: UUID
    status: str
    summary: str
    details: dict[str, JsonValue]
    created_at: datetime
    updated_at: datetime


class TimelineItem(StrictReportModel):
    type: str
    timestamp: datetime
    record_id: UUID
    summary: str


class FinalReportContent(StrictReportModel):
    schema_version: Literal["v0"]
    incident_summary: IncidentReportSection
    root_cause: RootCauseReportSection | None
    hypotheses: list[HypothesisReportItem]
    key_evidence: list[EvidenceReportItem]
    recommended_action: RecommendedActionReportSection | None
    action: ActionReportSection | None
    approval: ApprovalReportSection | None
    execution: ExecutionReportSection | None
    verification: VerificationReportSection | None
    timeline: list[TimelineItem] = Field(min_length=1)
    final_status: Literal["RESOLVED", "NEEDS_MANUAL_ACTION"]


class FinalReportError(RuntimeError):
    """The authoritative facts cannot safely form a final report."""


class FinalReportService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def generate(self, state: AgentState) -> ReportOutcome:
        incident = self._session.get(Incident, state["incident"].id)
        if incident is None or incident.status not in {"RESOLVED", "NEEDS_MANUAL_ACTION"}:
            raise FinalReportError("Incident is not terminal")
        content = self._content_for(incident, state)
        existing = self._session.scalar(select(Report).where(Report.incident_id == incident.id))
        if existing is not None:
            self._validate_existing(existing, content)
            return ReportOutcome(
                report_id=existing.id, incident_id=incident.id, final_status=incident.status
            )
        report = Report(
            incident_id=incident.id,
            content=content.model_dump(mode="json"),
            root_cause=content.root_cause.root_cause if content.root_cause else None,
        )
        self._session.add(report)
        self._session.commit()
        self._session.refresh(report)
        return ReportOutcome(
            report_id=report.id,
            incident_id=incident.id,
            final_status=incident.status,
        )

    def _content_for(self, incident: Incident, state: AgentState) -> FinalReportContent:
        policy = state["policy_outcome"]
        action = (
            self._session.get(Action, policy.action_id)
            if policy and policy.action_id
            else None
        )
        approval_state = state["approval_outcome"]
        approval = (
            self._session.get(Approval, approval_state.approval_id)
            if isinstance(approval_state, ApprovalOutcome)
            else None
        )
        verification_state = state["verification_outcome"]
        verification = (
            self._session.get(Verification, verification_state.verification_id)
            if isinstance(verification_state, VerificationOutcome)
            and verification_state.verification_id is not None
            else None
        )
        self._validate_chain(incident, state, action, approval, verification)
        evidence = {item.id: item for item in state["evidence"]}
        referenced = _referenced_evidence(state)
        if not referenced.issubset(evidence):
            raise FinalReportError("Report references unknown Evidence")
        conclusion = state["final_conclusion"]
        proposed = state["proposed_action"]
        return FinalReportContent(
            schema_version="v0",
            incident_summary=IncidentReportSection(
                incident_id=incident.id,
                service=incident.service,
                environment=incident.environment,
                description=incident.description,
                time_range_start=incident.time_range_start,
                time_range_end=incident.time_range_end,
                final_status=incident.status,
                created_at=incident.created_at,
            ),
            root_cause=(RootCauseReportSection(**conclusion.model_dump()) if conclusion else None),
            hypotheses=[HypothesisReportItem(**item.model_dump()) for item in state["hypotheses"]],
            key_evidence=[
                EvidenceReportItem(
                    id=evidence[evidence_id].id,
                    evidence_type=evidence[evidence_id].evidence_type,
                    source=evidence[evidence_id].source,
                    summary=evidence[evidence_id].summary,
                    reference=evidence[evidence_id].reference,
                )
                for evidence_id in sorted(referenced, key=str)
            ],
            recommended_action=(
                RecommendedActionReportSection(
                    **proposed.model_dump(exclude={"parameters"})
                )
                if proposed is not None
                else None
            ),
            action=_action_section(action),
            approval=_approval_section(approval),
            execution=_execution_section(state),
            verification=_verification_section(verification),
            timeline=_timeline(incident, action, approval, verification),
            final_status=incident.status,
        )

    def _validate_chain(
        self,
        incident: Incident,
        state: AgentState,
        action: Action | None,
        approval: Approval | None,
        verification: Verification | None,
    ) -> None:
        approval_state = state["approval_outcome"]
        execution = state["execution_outcome"]
        verification_state = state["verification_outcome"]
        if action is not None and action.incident_id != incident.id:
            raise FinalReportError("Action binding mismatch")
        if approval is not None and (action is None or approval.action_id != action.id):
            raise FinalReportError("Approval binding mismatch")
        if verification is not None and (action is None or verification.action_id != action.id):
            raise FinalReportError("Verification binding mismatch")
        if isinstance(approval_state, ApprovalOutcome) and (
            action is None
            or approval is None
            or approval.action_id != approval_state.action_id
            or approval.status != approval_state.status.value
        ):
            raise FinalReportError("Approval checkpoint mismatch")
        if execution is not None:
            if execution.status.value == "success":
                if (
                    action is None
                    or approval is None
                    or execution.action_id != action.id
                    or execution.approval_id != approval.id
                ):
                    raise FinalReportError("Execution binding mismatch")
                parameters = ActionExecutionParameters.model_validate(action.parameters)
                if (
                    execution.service != parameters.service
                    or execution.environment != parameters.environment
                    or execution.target_version != parameters.target_version
                ):
                    raise FinalReportError("Execution parameters mismatch")
            else:
                self._validate_failed_execution(execution, action, approval)
        if isinstance(verification_state, VerificationOutcome) and (
            action is None
            or verification is None
            or verification.id != verification_state.verification_id
            or verification.action_id != verification_state.action_id
            or verification.status != verification_state.status.value
        ):
            raise FinalReportError("Verification checkpoint mismatch")
        if verification is not None and (
            (verification.status == "PASS") != (incident.status == "RESOLVED")
        ):
            raise FinalReportError("Verification status mismatch")
        if incident.status == "RESOLVED" and (
            action is None
            or approval is None
            or verification is None
            or action.status != "EXECUTED"
            or approval.status != "APPROVED"
            or verification.status != "PASS"
            or execution is None
            or execution.status.value != "success"
            or not isinstance(verification_state, VerificationOutcome)
        ):
            raise FinalReportError("Resolved report lacks a verified execution chain")

    @staticmethod
    def _validate_failed_execution(
        execution: ActionExecutionOutcome,
        action: Action | None,
        approval: Approval | None,
    ) -> None:
        """Retain only failure facts which still bind to authoritative Action records."""
        if execution.executed:
            raise FinalReportError("Failed execution cannot report execution")
        if execution.action_id is not None and (
            action is None or execution.action_id != action.id
        ):
            raise FinalReportError("Failed execution Action binding mismatch")
        if execution.approval_id is not None and (
            approval is None or execution.approval_id != approval.id
        ):
            raise FinalReportError("Failed execution Approval binding mismatch")
        if any(
            value is not None
            for value in (execution.service, execution.environment, execution.target_version)
        ):
            if action is None:
                raise FinalReportError("Failed execution parameters lack an Action")
            parameters = ActionExecutionParameters.model_validate(action.parameters)
            if (
                execution.service is not None and execution.service != parameters.service
            ) or (
                execution.environment is not None
                and execution.environment != parameters.environment
            ) or (
                execution.target_version is not None
                and execution.target_version != parameters.target_version
            ):
                raise FinalReportError("Failed execution parameters mismatch")

    def _validate_existing(self, report: Report, expected: FinalReportContent) -> None:
        try:
            existing = FinalReportContent.model_validate(report.content)
        except Exception as error:
            raise FinalReportError("Existing Report content is invalid") from error
        if report.incident_id != expected.incident_summary.incident_id or any(
            (
                existing.final_status != expected.final_status,
                existing.root_cause != expected.root_cause,
                existing.action != expected.action,
                existing.approval != expected.approval,
                existing.execution != expected.execution,
                existing.verification != expected.verification,
            )
        ):
            raise FinalReportError("Existing Report conflicts with authoritative facts")


def _referenced_evidence(state: AgentState) -> set[UUID]:
    referenced: set[UUID] = set()
    for item in state["hypotheses"]:
        referenced.update(item.supporting_evidence_ids)
        referenced.update(item.contradicting_evidence_ids)
    if state["final_conclusion"] is not None:
        referenced.update(state["final_conclusion"].supporting_evidence_ids)
        referenced.update(state["final_conclusion"].contradicting_evidence_ids)
    if state["proposed_action"] is not None:
        referenced.update(state["proposed_action"].supporting_evidence_ids)
    return referenced


def _action_section(action: Action | None) -> ActionReportSection | None:
    return None if action is None else ActionReportSection(
        action_id=action.id,
        action_type=action.action_type,
        status=action.status,
        parameters=action.parameters,
        created_at=action.created_at,
        executed_at=action.executed_at,
    )


def _approval_section(approval: Approval | None) -> ApprovalReportSection | None:
    return None if approval is None else ApprovalReportSection(
        approval_id=approval.id,
        action_id=approval.action_id,
        status=approval.status,
        created_at=approval.created_at,
        updated_at=approval.updated_at,
    )


def _execution_section(state: AgentState) -> ExecutionReportSection | None:
    execution = state["execution_outcome"]
    return None if execution is None else ExecutionReportSection(
        action_id=execution.action_id,
        approval_id=execution.approval_id,
        status=execution.status.value,
        service=execution.service,
        environment=execution.environment,
        target_version=execution.target_version,
        executed=execution.executed,
    )


def _verification_section(verification: Verification | None) -> VerificationReportSection | None:
    return None if verification is None else VerificationReportSection(
        verification_id=verification.id,
        action_id=verification.action_id,
        status=verification.status,
        summary=verification.summary,
        details=verification.details,
        created_at=verification.created_at,
        updated_at=verification.updated_at,
    )


def _timeline(
    incident: Incident,
    action: Action | None,
    approval: Approval | None,
    verification: Verification | None,
) -> list[TimelineItem]:
    items = [
        TimelineItem(
            type="incident_created",
            timestamp=incident.created_at,
            record_id=incident.id,
            summary="Incident created",
        )
    ]
    records = (
        ("action_created", action, getattr(action, "created_at", None)),
        ("approval_recorded", approval, getattr(approval, "created_at", None)),
        ("action_executed", action, getattr(action, "executed_at", None)),
        _verification_timeline_entry(verification),
    )
    for name, record, timestamp in records:
        if record is not None and timestamp is not None:
            items.append(
                TimelineItem(
                    type=name,
                    timestamp=timestamp,
                    record_id=record.id,
                    summary=name.replace("_", " "),
                )
            )
    return sorted(items, key=lambda item: item.timestamp)


def _verification_timeline_entry(
    verification: Verification | None,
) -> tuple[str, Verification | None, datetime | None]:
    if verification is None:
        return "verification_recorded", None, None
    completed_at = verification.details.get("verification_completed_at")
    if isinstance(completed_at, str):
        try:
            parsed = datetime.fromisoformat(completed_at)
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                return "verification_completed", verification, parsed
        except ValueError:
            pass
    return "verification_recorded", verification, verification.created_at


def final_report_node(state: AgentState, service: FinalReportService) -> AgentState:
    outcome = service.generate(state)
    return {**state, "report_outcome": outcome}
