"""Tests for grounded, non-executable resolution proposals without external calls."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from devsupport_backend.agent.llm import LLMError
from devsupport_backend.agent.resolution_proposal import (
    ResolutionProposalError,
    resolution_proposal_node,
)
from devsupport_backend.agent.state import (
    AgentStage,
    AgentState,
    EvaluationDecision,
    EvidenceContext,
    HypothesisContext,
    HypothesisStatus,
    ToolHistoryEntry,
    create_initial_agent_state,
)
from devsupport_backend.models import Incident
from devsupport_backend.tools.schemas import ToolStatus


class FakeLLMClient:
    """Captures proposal prompts and returns deterministic, test-only output."""

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


def build_resolution_state() -> tuple[AgentState, EvidenceContext, HypothesisContext]:
    """Create a concluded investigation with one evidence-backed confirmed hypothesis."""
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
    evidence = EvidenceContext(
        evidence_type="deployment_facts",
        source="get_deployment_history",
        summary="A recent catalog deployment is within the incident time window.",
        data={"current_version": "v2.0.0", "previous_version": "v1.9.0"},
    )
    hypothesis = HypothesisContext(
        summary="A recent catalog deployment introduced the observed request failure.",
        status=HypothesisStatus.CONFIRMED,
        confidence=0.88,
        supporting_evidence_ids=[evidence.id],
        next_check="Use the confirmed finding to prepare a controlled proposal.",
    )
    state["evidence"] = [evidence]
    state["hypotheses"] = [hypothesis]
    state["tool_history"] = [
        ToolHistoryEntry(
            tool_name="get_deployment_history",
            tool_arguments={"service": "catalog-service", "environment": "staging"},
            status=ToolStatus.SUCCESS,
            duration_ms=2.0,
            evidence_ids=[evidence.id],
        )
    ]
    state["current_stage"] = AgentStage.EVIDENCE_EVALUATION
    state["evaluation_decision"] = EvaluationDecision.CONCLUDE
    return state, evidence, hypothesis


def proposal_response(
    hypothesis: HypothesisContext,
    evidence: EvidenceContext,
    *,
    action_type: str = "manual_action",
    recommended_action: str = "Ask an operator to review the confirmed deployment evidence.",
) -> str:
    """Build a valid high-level proposal without executable deployment parameters."""
    return json.dumps(
        {
            "confirmed_hypothesis_id": str(hypothesis.id),
            "root_cause": hypothesis.summary,
            "confidence": 0.88,
            "recommended_action": recommended_action,
            "action_type": action_type,
            "reason": "The cited deployment fact supports the confirmed hypothesis.",
            "supporting_evidence_ids": [str(evidence.id)],
            "risk": "Any operational change requires policy review and human approval.",
        }
    )


def test_concluded_confirmed_hypothesis_creates_grounded_final_conclusion_and_action() -> None:
    state, evidence, hypothesis = build_resolution_state()
    client = FakeLLMClient(proposal_response(hypothesis, evidence))

    updated = resolution_proposal_node(state, client)

    assert updated["current_stage"] is AgentStage.CONCLUSION
    assert updated["final_conclusion"] is not None
    assert updated["final_conclusion"].root_cause == hypothesis.summary
    assert updated["final_conclusion"].supporting_evidence_ids == [evidence.id]
    assert updated["proposed_action"] is not None
    assert updated["proposed_action"].supporting_evidence_ids == [evidence.id]
    assert updated["proposed_action"].parameters == {}
    assert updated["hypotheses"] == state["hypotheses"]
    assert updated["evidence"] == state["evidence"]
    assert updated["tool_history"] == state["tool_history"]
    assert updated["tool_call_count"] == state["tool_call_count"] == 0
    assert client.user_prompt is not None
    assert set(json.loads(client.user_prompt)) == {
        "incident",
        "hypotheses",
        "evidence",
        "tool_history",
    }


def test_rollback_suggestion_is_only_a_non_executable_proposed_action() -> None:
    state, evidence, hypothesis = build_resolution_state()
    response = proposal_response(
        hypothesis,
        evidence,
        action_type="rollback_deployment",
        recommended_action="Propose a controlled rollback after policy review and approval.",
    )

    updated = resolution_proposal_node(state, FakeLLMClient(response))

    assert updated["proposed_action"] is not None
    assert updated["proposed_action"].action_type == "rollback_deployment"
    assert updated["proposed_action"].parameters == {}
    assert updated["current_stage"] is AgentStage.CONCLUSION


def test_manual_action_proposal_is_structured() -> None:
    state, evidence, hypothesis = build_resolution_state()

    updated = resolution_proposal_node(
        state,
        FakeLLMClient(proposal_response(hypothesis, evidence)),
    )

    assert updated["proposed_action"] is not None
    assert updated["proposed_action"].action_type == "manual_action"
    assert updated["proposed_action"].risk


@pytest.mark.parametrize("invalid_field", ["supporting_evidence_ids", "confirmed_hypothesis_id"])
def test_unknown_evidence_or_hypothesis_is_rejected_without_partial_state(
    invalid_field: str,
) -> None:
    state, evidence, hypothesis = build_resolution_state()
    payload = json.loads(proposal_response(hypothesis, evidence))
    payload[invalid_field] = [str(uuid4())] if invalid_field.endswith("ids") else str(uuid4())

    with pytest.raises(ResolutionProposalError):
        resolution_proposal_node(state, FakeLLMClient(json.dumps(payload)))

    assert state["final_conclusion"] is None
    assert state["proposed_action"] is None
    assert state["current_stage"] is AgentStage.EVIDENCE_EVALUATION


def test_no_confirmed_hypothesis_or_ungrounded_execution_parameters_are_rejected() -> None:
    state, evidence, hypothesis = build_resolution_state()
    state["hypotheses"] = [hypothesis.model_copy(update={"status": HypothesisStatus.SUPPORTED})]

    with pytest.raises(ResolutionProposalError, match="CONFIRMED hypothesis"):
        resolution_proposal_node(state, FakeLLMClient(proposal_response(hypothesis, evidence)))

    state["hypotheses"] = [hypothesis]
    payload = json.loads(proposal_response(hypothesis, evidence))
    payload["parameters"] = {"target_version": "v9.9.9"}
    with pytest.raises(ResolutionProposalError, match="output validation failed"):
        resolution_proposal_node(state, FakeLLMClient(json.dumps(payload)))

    assert state["final_conclusion"] is None
    assert state["proposed_action"] is None


def test_unknown_action_type_is_rejected_at_the_structured_output_boundary() -> None:
    state, evidence, hypothesis = build_resolution_state()

    with pytest.raises(ResolutionProposalError, match="output validation failed"):
        resolution_proposal_node(
            state,
            FakeLLMClient(proposal_response(hypothesis, evidence, action_type="restart_service")),
        )

    assert state["final_conclusion"] is None
    assert state["proposed_action"] is None


def test_malformed_provider_output_and_non_conclude_state_do_not_propose_resolution() -> None:
    state, evidence, hypothesis = build_resolution_state()

    with pytest.raises(ResolutionProposalError, match="output validation failed"):
        resolution_proposal_node(state, FakeLLMClient("not JSON"))
    with pytest.raises(ResolutionProposalError, match="provider failed"):
        resolution_proposal_node(state, FakeLLMClient(LLMError("network unavailable")))

    for decision in (EvaluationDecision.CONTINUE, EvaluationDecision.NEEDS_MANUAL_ACTION):
        state["evaluation_decision"] = decision
        client = FakeLLMClient(proposal_response(hypothesis, evidence))
        updated = resolution_proposal_node(state, client)
        assert updated is state
        assert client.user_prompt is None

    assert state["final_conclusion"] is None
    assert state["proposed_action"] is None
