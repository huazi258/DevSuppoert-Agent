"""Deterministic normalization and readiness checks for incident intake."""

from __future__ import annotations

from datetime import datetime

from devsupport_backend.agent.state import (
    AgentStage,
    AgentState,
    IncidentContext,
    IntakeDecision,
    IntakeOutcome,
)


def intake_node(state: AgentState) -> AgentState:
    """Return an intake-only state update without I/O or investigation work."""
    incident = state["incident"]
    service = _normalize_text(incident.service)
    environment = _normalize_text(incident.environment)
    description = _normalize_text(incident.description)
    symptoms = _normalize_symptoms(incident.symptoms)
    if not symptoms and description:
        symptoms = [description]

    missing_information = _find_missing_information(
        service=service,
        environment=environment,
        time_range_start=incident.time_range_start,
        time_range_end=incident.time_range_end,
        symptoms=symptoms,
    )
    if missing_information:
        outcome = IntakeOutcome(
            decision=IntakeDecision.NEEDS_INFORMATION,
            missing_information=missing_information,
        )
        return _apply_outcome(state, incident=incident, outcome=outcome)

    normalized_incident = IncidentContext(
        id=incident.id,
        service=service,
        environment=environment,
        description=description,
        time_range_start=incident.time_range_start,
        time_range_end=incident.time_range_end,
        symptoms=symptoms,
    )
    outcome = IntakeOutcome(decision=IntakeDecision.READY)
    return _apply_outcome(state, incident=normalized_incident, outcome=outcome)


def _apply_outcome(
    state: AgentState, *, incident: IncidentContext, outcome: IntakeOutcome
) -> AgentState:
    """Apply only the fields owned by Intake, using the validated outcome contract."""
    return {
        **state,
        "incident": incident,
        "current_stage": (
            AgentStage.RETRIEVAL
            if outcome.decision is IntakeDecision.READY
            else AgentStage.INTAKE
        ),
        "intake_decision": outcome.decision,
        "missing_information": outcome.missing_information,
    }


def _find_missing_information(
    *,
    service: str,
    environment: str,
    time_range_start: datetime | object,
    time_range_end: datetime | object,
    symptoms: list[str],
) -> list[str]:
    """Identify only minimum intake facts; this makes no diagnosis inference."""
    missing_information: list[str] = []
    if not service:
        missing_information.append("service")
    if not environment:
        missing_information.append("environment")
    if not _is_timezone_aware(time_range_start) or not _is_timezone_aware(time_range_end):
        missing_information.append("timezone-aware time range")
    elif time_range_start > time_range_end:
        missing_information.append("valid time range")
    if not symptoms:
        missing_information.append("symptom context")
    return missing_information


def _normalize_symptoms(values: list[str] | object) -> list[str]:
    """Trim and de-duplicate symptom facts while preserving their first wording."""
    if not isinstance(values, list):
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        symptom = _normalize_text(value)
        comparison_key = symptom.casefold()
        if symptom and comparison_key not in seen:
            normalized.append(symptom)
            seen.add(comparison_key)
    return normalized


def _normalize_text(value: str | object) -> str:
    """Apply the project's lightweight text normalization without semantic changes."""
    return value.strip() if isinstance(value, str) else ""


def _is_timezone_aware(value: datetime | object) -> bool:
    """Keep the incident window aligned with the timezone-aware API contract."""
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )
