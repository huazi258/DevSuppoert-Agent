"""Deterministic final Incident report projection; no new investigation facts."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from devsupport_backend.agent.state import AgentState, ReportOutcome
from devsupport_backend.models import Action, Approval, Incident, Report, Verification


class FinalReportError(RuntimeError):
    pass


class FinalReportService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def generate(self, state: AgentState) -> ReportOutcome:
        incident = self._session.get(Incident, state["incident"].id)
        if incident is None or incident.status not in {"RESOLVED", "NEEDS_MANUAL_ACTION"}:
            raise FinalReportError("Incident is not terminal")
        existing = self._session.scalar(select(Report).where(Report.incident_id == incident.id))
        if existing is not None:
            return ReportOutcome(
                report_id=existing.id, incident_id=incident.id, final_status=incident.status
            )
        action = self._session.scalar(
            select(Action)
            .where(Action.incident_id == incident.id)
            .order_by(Action.created_at.desc())
        )
        approval = self._session.scalar(select(Approval).where(Approval.incident_id == incident.id))
        verification = self._session.scalar(
            select(Verification).where(Verification.incident_id == incident.id)
        )
        if action and action.incident_id != incident.id:
            raise FinalReportError("Action binding mismatch")
        if approval and (action is None or approval.action_id != action.id):
            raise FinalReportError("Approval binding mismatch")
        if verification and (action is None or verification.action_id != action.id):
            raise FinalReportError("Verification binding mismatch")
        if verification and ((verification.status == "PASS") != (incident.status == "RESOLVED")):
            raise FinalReportError("Verification status mismatch")
        evidence = {item.id: item for item in state["evidence"]}
        referenced = set()
        for item in state["hypotheses"]:
            referenced.update(item.supporting_evidence_ids)
            referenced.update(item.contradicting_evidence_ids)
        if state["final_conclusion"]:
            referenced.update(state["final_conclusion"].supporting_evidence_ids)
            referenced.update(state["final_conclusion"].contradicting_evidence_ids)
        if state["proposed_action"]:
            referenced.update(state["proposed_action"].supporting_evidence_ids)
        if not referenced.issubset(evidence):
            raise FinalReportError("Report references unknown Evidence")
        content = {
            "schema_version": "v0",
            "incident_summary": {
                "incident_id": str(incident.id),
                "service": incident.service,
                "environment": incident.environment,
                "description": incident.description,
                "time_range_start": incident.time_range_start,
                "time_range_end": incident.time_range_end,
                "final_status": incident.status,
                "created_at": incident.created_at,
            },
            "root_cause": state["final_conclusion"].model_dump(mode="json")
            if state["final_conclusion"]
            else None,
            "hypotheses": [x.model_dump(mode="json") for x in state["hypotheses"]],
            "key_evidence": [
                {
                    "id": str(evidence[i].id),
                    "evidence_type": evidence[i].evidence_type,
                    "source": evidence[i].source,
                    "summary": evidence[i].summary,
                    "reference": evidence[i].reference,
                }
                for i in referenced
            ],
            "recommended_action": state["proposed_action"].model_dump(
                mode="json", exclude={"parameters"}
            )
            if state["proposed_action"]
            else None,
            "action": _action(action),
            "approval": _approval(approval),
            "execution": state["execution_outcome"].model_dump(mode="json")
            if state["execution_outcome"]
            else None,
            "verification": _verification(verification),
            "timeline": [],
            "final_status": incident.status,
        }
        report = Report(
            incident_id=incident.id,
            content=content,
            root_cause=state["final_conclusion"].root_cause if state["final_conclusion"] else None,
        )
        self._session.add(report)
        self._session.commit()
        self._session.refresh(report)
        return ReportOutcome(
            report_id=report.id, incident_id=incident.id, final_status=incident.status
        )


def _action(a):
    return (
        None
        if a is None
        else {
            "action_id": str(a.id),
            "action_type": a.action_type,
            "status": a.status,
            "parameters": a.parameters,
            "created_at": a.created_at,
            "executed_at": a.executed_at,
        }
    )


def _approval(a):
    return (
        None
        if a is None
        else {
            "approval_id": str(a.id),
            "action_id": str(a.action_id),
            "status": a.status,
            "created_at": a.created_at,
            "updated_at": a.updated_at,
        }
    )


def _verification(v):
    return (
        None
        if v is None
        else {
            "verification_id": str(v.id),
            "action_id": str(v.action_id),
            "status": v.status,
            "summary": v.summary,
            "details": v.details,
            "created_at": v.created_at,
            "updated_at": v.updated_at,
        }
    )
