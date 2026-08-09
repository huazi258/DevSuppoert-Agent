"""Tests for the standalone Agent workflow-runtime state contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from devsupport_backend.agent.state import (
    MAX_EVIDENCE_DATA_SERIALIZED_BYTES,
    AgentStage,
    EvidenceContext,
    HypothesisContext,
    HypothesisStatus,
    IntakeDecision,
    IntakeOutcome,
    PendingToolCall,
    ToolHistoryEntry,
    agent_state_to_checkpoint_payload,
    create_initial_agent_state,
)
from devsupport_backend.models import Incident
from devsupport_backend.tools.schemas import ToolError, ToolStatus


def build_incident() -> Incident:
    """Construct an ordinary persisted-model projection without a Session."""
    started_at = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
    return Incident(
        id=uuid4(),
        service="order-service",
        environment="local",
        description="POST /orders is failing.",
        time_range_start=started_at,
        time_range_end=started_at + timedelta(minutes=5),
    )


def test_initial_agent_state_has_neutral_intake_defaults() -> None:
    incident = build_incident()

    state = create_initial_agent_state(incident, symptoms=["HTTP 500"])

    assert state["incident"].id == incident.id
    assert state["incident"].symptoms == ["HTTP 500"]
    assert state["current_stage"] is AgentStage.INTAKE
    assert state["hypotheses"] == []
    assert state["evidence"] == []
    assert state["current_goal"] is None
    assert state["pending_tool_call"] is None
    assert state["tool_history"] == []
    assert state["investigation_round"] == 0
    assert state["tool_call_count"] == 0
    assert state["intake_decision"] is None
    assert state["missing_information"] == []
    assert state["evaluation_decision"] is None
    assert state["proposed_action"] is None
    assert state["final_conclusion"] is None
    assert state["policy_outcome"] is None
    assert state["approval_outcome"] is None


def test_hypothesis_validates_fixed_status_and_confidence_range() -> None:
    hypothesis = HypothesisContext(
        summary="A deployment changed order-service configuration.",
        status=HypothesisStatus.SUPPORTED,
        confidence=0.75,
    )

    assert hypothesis.status is HypothesisStatus.SUPPORTED
    assert hypothesis.confidence == 0.75
    with pytest.raises(ValidationError):
        HypothesisContext(summary="Invalid confidence", confidence=1.01)
    with pytest.raises(ValidationError):
        HypothesisContext(summary="Invalid status", status="OPEN")  # type: ignore[arg-type]


def test_evidence_has_a_stable_id_and_json_reference_data() -> None:
    evidence = EvidenceContext(
        evidence_type="log_pattern",
        source="query_logs",
        summary="The error pattern occurred twice.",
        data={"count": 2, "trace_ids": ["abc123"]},
        reference="log:order-service:abc123",
    )

    assert evidence.id
    assert evidence.reference == "log:order-service:abc123"
    assert evidence.model_dump(mode="json")["data"] == {"count": 2, "trace_ids": ["abc123"]}


def test_evidence_rejects_oversized_serialized_data() -> None:
    with pytest.raises(ValidationError, match="evidence data exceeds"):
        EvidenceContext(
            evidence_type="raw_log_payload",
            source="query_logs",
            summary="This must remain a concise evidence summary.",
            data={"raw_logs": "x" * MAX_EVIDENCE_DATA_SERIALIZED_BYTES},
        )


def test_intake_outcome_validates_decisions_and_missing_information() -> None:
    ready = IntakeOutcome(decision="READY")
    needs_information = IntakeOutcome(
        decision="NEEDS_INFORMATION",
        missing_information=["  Exact incident time range  "],
    )

    assert ready.decision is IntakeDecision.READY
    assert ready.missing_information == []
    assert needs_information.decision is IntakeDecision.NEEDS_INFORMATION
    assert needs_information.missing_information == ["Exact incident time range"]
    with pytest.raises(ValidationError):
        IntakeOutcome(decision="CONTINUE")
    with pytest.raises(ValidationError, match="missing_information must not contain blank values"):
        IntakeOutcome(decision="NEEDS_INFORMATION", missing_information=["   "])


def test_pending_tool_call_accepts_only_registered_tool_names() -> None:
    pending = PendingToolCall(
        investigation_goal="Check current order-service errors.",
        tool_name="query_logs",
        tool_arguments={"service": "order-service", "environment": "local"},
        reason="Logs can test the configuration hypothesis.",
    )

    assert pending.tool_name.value == "query_logs"
    with pytest.raises(ValidationError):
        PendingToolCall(
            investigation_goal="Run arbitrary command.",
            tool_name="run_shell",
            reason="Not a permitted tool.",
        )


def test_state_checkpoint_payload_is_json_serializable_without_orm_or_session() -> None:
    state = create_initial_agent_state(build_incident())
    evidence = EvidenceContext(
        evidence_type="metric_snapshot",
        source="query_metrics",
        summary="Order-service error rate is non-zero.",
        data={"error_rate": 0.5},
    )
    state["evidence"].append(evidence)
    state["intake_decision"] = IntakeDecision.NEEDS_INFORMATION
    state["missing_information"] = ["Exact incident time range"]
    state["tool_history"].append(
        ToolHistoryEntry(
            tool_name="query_metrics",
            tool_arguments={"service": "order-service", "environment": "local"},
            status=ToolStatus.SUCCESS,
            duration_ms=12.5,
            evidence_ids=[evidence.id],
        )
    )

    payload = agent_state_to_checkpoint_payload(state)

    assert isinstance(payload["incident"], dict)
    assert json.loads(json.dumps(payload))["evidence"][0]["id"] == str(evidence.id)
    assert payload["intake_decision"] == "NEEDS_INFORMATION"
    assert payload["missing_information"] == ["Exact incident time range"]


def test_tool_history_requires_a_structured_error_for_failure() -> None:
    with pytest.raises(ValidationError, match="must include an error"):
        ToolHistoryEntry(tool_name="query_logs", status=ToolStatus.FAILURE)

    entry = ToolHistoryEntry(
        tool_name="query_logs",
        status=ToolStatus.UNAVAILABLE,
        error=ToolError(code="unavailable", message="Fault Lab is not running", retryable=True),
    )

    assert entry.error is not None
