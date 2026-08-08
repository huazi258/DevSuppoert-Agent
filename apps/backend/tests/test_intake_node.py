"""Tests for the pure, deterministic incident Intake node."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from devsupport_backend.agent.nodes.intake import intake_node
from devsupport_backend.agent.state import (
    AgentStage,
    IntakeDecision,
    create_initial_agent_state,
)
from devsupport_backend.models import Incident


def build_incident(*, description: str = "POST /orders returns 500 after deployment") -> Incident:
    """Construct a valid Incident projection without a database Session."""
    started_at = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
    return Incident(
        id=uuid4(),
        service="order-service",
        environment="local",
        description=description,
        time_range_start=started_at,
        time_range_end=started_at + timedelta(minutes=5),
    )


def test_intake_marks_a_valid_incident_ready_with_description_symptom() -> None:
    state = create_initial_agent_state(build_incident())

    result = intake_node(state)

    assert result["intake_decision"] is IntakeDecision.READY
    assert result["current_stage"] is AgentStage.RETRIEVAL
    assert result["missing_information"] == []
    assert result["incident"].symptoms == ["POST /orders returns 500 after deployment"]


def test_intake_normalizes_existing_symptoms_without_changing_their_meaning() -> None:
    state = create_initial_agent_state(
        build_incident(),
        symptoms=["  POST /orders returns 500  ", "post /orders returns 500", "Payment was called"],
    )

    result = intake_node(state)

    assert result["incident"].symptoms == ["POST /orders returns 500", "Payment was called"]
    assert result["intake_decision"] is IntakeDecision.READY


def test_intake_keeps_symptoms_as_facts_without_root_cause_inference() -> None:
    description = "POST /orders returns 500 after deployment"
    state = create_initial_agent_state(build_incident(description=description))

    result = intake_node(state)

    assert result["incident"].symptoms == [description]
    assert result["hypotheses"] == []
    assert result["evidence"] == []
    assert result["proposed_action"] is None
    assert result["final_conclusion"] is None


def test_intake_uses_needs_information_for_defensively_incomplete_state() -> None:
    state = create_initial_agent_state(build_incident())
    state["incident"].service = " "

    result = intake_node(state)

    assert result["intake_decision"] is IntakeDecision.NEEDS_INFORMATION
    assert result["missing_information"] == ["service"]
    assert result["current_stage"] is AgentStage.INTAKE


def test_intake_changes_only_its_owned_state_fields() -> None:
    state = create_initial_agent_state(build_incident())
    state["investigation_round"] = 2
    state["tool_call_count"] = 3

    result = intake_node(state)

    assert result["hypotheses"] == []
    assert result["evidence"] == []
    assert result["tool_history"] == []
    assert result["investigation_round"] == 2
    assert result["tool_call_count"] == 3
    assert result["evaluation_decision"] is None
    assert result["proposed_action"] is None
    assert result["final_conclusion"] is None
