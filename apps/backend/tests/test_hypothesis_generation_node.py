"""Tests for structured LLM hypothesis generation without external API calls."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from devsupport_backend.agent.llm import LLMError
from devsupport_backend.agent.nodes.hypothesis_generation import (
    HypothesisGenerationError,
    hypothesis_generation_node,
)
from devsupport_backend.agent.state import (
    AgentStage,
    AgentState,
    EvidenceContext,
    HypothesisStatus,
    create_initial_agent_state,
)
from devsupport_backend.models import Incident


class FakeLLMClient:
    """Captures prompts and returns deterministic test-only provider output."""

    def __init__(self, response: str | Exception) -> None:
        self.response = response
        self.system_prompt: str | None = None
        self.user_prompt: str | None = None

    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def build_generation_state() -> tuple[AgentState, EvidenceContext]:
    """Create a state containing one trusted-shaped knowledge evidence item."""
    started_at = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
    incident = Incident(
        id=uuid4(),
        service="catalog-service",
        environment="staging",
        description="The catalog endpoint returns errors after a recent change.",
        time_range_start=started_at,
        time_range_end=started_at + timedelta(minutes=5),
    )
    state = create_initial_agent_state(incident, symptoms=["Catalog endpoint returns errors"])
    knowledge_evidence = EvidenceContext(
        evidence_type="knowledge_retrieval",
        source="search_knowledge",
        summary="Runbook recommends comparing request errors with dependency signals.",
        data={
            "document_id": str(uuid4()),
            "chunk_id": str(uuid4()),
            "document_type": "runbook",
            "source": "knowledge/runbooks/catalog-errors.md",
            "section": "Initial checks",
            "fusion_score": 0.05,
            "citation": {"id": "catalog-runbook#initial-checks"},
        },
        reference="knowledge/runbooks/catalog-errors.md#initial-checks",
    )
    non_knowledge_evidence = EvidenceContext(
        evidence_type="incident_fact",
        source="intake",
        summary="The caller reported errors.",
    )
    state["evidence"] = [knowledge_evidence, non_knowledge_evidence]
    state["current_stage"] = AgentStage.HYPOTHESIS_GENERATION
    return state, knowledge_evidence


def valid_response(evidence_id: str) -> str:
    """Return the required 2-item structured hypothesis output."""
    return json.dumps(
        {
            "hypotheses": [
                {
                    "summary": "A recent change may affect catalog request handling.",
                    "confidence": 0.6,
                    "supporting_evidence_ids": [evidence_id],
                    "next_check": "Compare recent change timing with the error window.",
                },
                {
                    "summary": "A dependency signal may correlate with catalog errors.",
                    "confidence": 0.35,
                    "supporting_evidence_ids": [],
                    "next_check": "Inspect dependency health evidence.",
                },
            ]
        }
    )


def test_generation_creates_valid_active_hypotheses_and_advances_stage() -> None:
    state, evidence = build_generation_state()
    client = FakeLLMClient(valid_response(str(evidence.id)))

    updated = hypothesis_generation_node(state, client)

    assert updated["current_stage"] is AgentStage.INVESTIGATION_PLANNING
    assert len(updated["hypotheses"]) == 2
    assert all(item.status is HypothesisStatus.ACTIVE for item in updated["hypotheses"])
    assert all(
        item.confidence is not None and 0 <= item.confidence <= 1
        for item in updated["hypotheses"]
    )
    assert updated["hypotheses"][0].supporting_evidence_ids == [evidence.id]
    assert updated["evidence"] == state["evidence"]
    assert updated["tool_history"] == state["tool_history"]
    assert updated["tool_call_count"] == state["tool_call_count"] == 0
    assert updated["investigation_round"] == state["investigation_round"] == 0
    assert updated["proposed_action"] is state["proposed_action"] is None
    assert updated["final_conclusion"] is state["final_conclusion"] is None


def test_generation_prompt_uses_only_incident_and_knowledge_evidence() -> None:
    state, evidence = build_generation_state()
    client = FakeLLMClient(valid_response(str(evidence.id)))

    hypothesis_generation_node(state, client)

    assert client.user_prompt is not None
    context = json.loads(client.user_prompt)
    assert set(context) == {"incident", "knowledge_evidence"}
    assert context["incident"]["service"] == "catalog-service"
    assert context["incident"]["environment"] == "staging"
    assert context["incident"]["symptoms"] == ["Catalog endpoint returns errors"]
    assert context["knowledge_evidence"] == [
        {
            "id": str(evidence.id),
            "summary": evidence.summary,
            "reference": evidence.reference,
            "document_id": evidence.data["document_id"],
            "chunk_id": evidence.data["chunk_id"],
            "document_type": "runbook",
            "source": "knowledge/runbooks/catalog-errors.md",
            "section": "Initial checks",
            "fusion_score": 0.05,
            "citation": {"id": "catalog-runbook#initial-checks"},
        }
    ]
    assert client.system_prompt is not None
    assert "knowledge-evidence" in client.system_prompt


@pytest.mark.parametrize(
    "response",
    [
        "not JSON",
        json.dumps({"hypotheses": []}),
        json.dumps(
            {
                "hypotheses": [
                    {
                        "summary": "Invalid confidence.",
                        "confidence": 1.1,
                        "supporting_evidence_ids": [],
                        "next_check": "Check another fact.",
                    },
                    {
                        "summary": "Second item.",
                        "confidence": 0.2,
                        "supporting_evidence_ids": [],
                        "next_check": "Check another fact.",
                    },
                ]
            }
        ),
    ],
)
def test_malformed_or_invalid_output_does_not_create_hypotheses(response: str) -> None:
    state, evidence = build_generation_state()
    client = FakeLLMClient(response)

    with pytest.raises(HypothesisGenerationError):
        hypothesis_generation_node(state, client)

    assert state["hypotheses"] == []
    assert state["current_stage"] is AgentStage.HYPOTHESIS_GENERATION
    assert state["evidence"] == [evidence, state["evidence"][1]]


def test_unknown_evidence_reference_and_provider_failure_leave_state_unchanged() -> None:
    state, evidence = build_generation_state()
    unknown_reference = valid_response(str(uuid4()))

    with pytest.raises(HypothesisGenerationError, match="unknown knowledge evidence"):
        hypothesis_generation_node(state, FakeLLMClient(unknown_reference))
    with pytest.raises(HypothesisGenerationError, match="provider failed"):
        hypothesis_generation_node(state, FakeLLMClient(LLMError("network unavailable")))

    assert state["hypotheses"] == []
    assert state["evidence"][0] == evidence
    assert state["tool_history"] == []
    assert state["tool_call_count"] == 0
    assert state["investigation_round"] == 0


def test_generation_skips_llm_outside_hypothesis_stage() -> None:
    state, evidence = build_generation_state()
    state["current_stage"] = AgentStage.RETRIEVAL
    client = FakeLLMClient(valid_response(str(evidence.id)))

    updated = hypothesis_generation_node(state, client)

    assert updated is state
    assert client.user_prompt is None
