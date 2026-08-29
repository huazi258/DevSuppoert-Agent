"""Tests for safe, structured LLM evidence evaluation without external calls."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from devsupport_backend.agent.evidence_evaluator import (
    EvidenceEvaluationError,
    LLMEvidenceEvaluator,
    _build_prompt_context,
    is_conclusion_eligible,
)
from devsupport_backend.agent.llm import LLMError
from devsupport_backend.agent.state import (
    AgentStage,
    AgentState,
    EvaluationDecision,
    EvidenceContext,
    HypothesisContext,
    HypothesisStatus,
    TerminalReason,
    ToolHistoryEntry,
    create_initial_agent_state,
)
from devsupport_backend.agent.workflow import (
    InvestigationLoopLimits,
    evidence_evaluation_node,
)
from devsupport_backend.models import Incident
from devsupport_backend.tools.schemas import ToolStatus


class FakeLLMClient:
    """Captures evaluation prompts and returns deterministic provider output."""

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


class RecordingEvaluator:
    def __init__(self, decision: EvaluationDecision) -> None:
        self.decision = decision
        self.calls = 0

    def evaluate(self, state: AgentState) -> EvaluationDecision:
        del state
        self.calls += 1
        return self.decision


def build_evaluation_state() -> tuple[AgentState, EvidenceContext, HypothesisContext]:
    """Create concise state facts for direct evaluator tests."""
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
        evidence_type="metric_snapshot",
        source="query_metrics",
        summary="Request errors increased during the incident window.",
        data={"error_rate": 0.4},
    )
    hypothesis = HypothesisContext(
        summary="A service-local condition may affect requests.",
        status=HypothesisStatus.ACTIVE,
        confidence=0.5,
        next_check="Inspect one more runtime signal.",
    )
    state["evidence"] = [evidence]
    state["hypotheses"] = [hypothesis]
    state["tool_history"] = [
        ToolHistoryEntry(
            tool_name="query_metrics",
            tool_arguments={"service": "catalog-service", "environment": "staging"},
            status=ToolStatus.SUCCESS,
            duration_ms=3.0,
            evidence_ids=[evidence.id],
        )
    ]
    state["current_stage"] = AgentStage.EVIDENCE_EVALUATION
    return state, evidence, hypothesis


def evaluation_response(decision: str) -> str:
    """Build the minimum required strict evaluation provider payload."""
    return json.dumps(
        {
            "decision": decision,
            "reason": "The current structured evidence supports this evaluation decision.",
        }
    )


def test_insufficient_evidence_can_continue_and_prompt_contains_only_state_facts() -> None:
    state, evidence, hypothesis = build_evaluation_state()
    client = FakeLLMClient(evaluation_response("CONTINUE"))

    decision = LLMEvidenceEvaluator(client).evaluate(state)

    assert decision.value == "CONTINUE"
    assert client.user_prompt is not None
    context = json.loads(client.user_prompt)
    assert set(context) == {
        "incident",
        "hypotheses",
        "evidence",
        "tool_history",
        "decision_contract",
    }
    assert context["incident"]["service"] == "catalog-service"
    assert context["hypotheses"][0]["id"] == str(hypothesis.id)
    assert context["evidence"][0]["id"] == str(evidence.id)
    assert context["tool_history"][0]["tool_name"] == "query_metrics"
    assert context["decision_contract"]["allowed_decisions"] == [
        "CONTINUE",
        "NEEDS_MANUAL_ACTION",
    ]
    assert context["decision_contract"]["conclude_allowed"] is False
    assert client.system_prompt is not None
    assert "untrusted" in client.system_prompt


def test_confirmed_hypothesis_with_real_supporting_evidence_can_conclude() -> None:
    state, evidence, hypothesis = build_evaluation_state()
    state["hypotheses"] = [
        hypothesis.model_copy(
            update={
                "status": HypothesisStatus.CONFIRMED,
                "supporting_evidence_ids": [evidence.id],
            }
        )
    ]

    contract = _build_prompt_context(state)["decision_contract"]
    decision = LLMEvidenceEvaluator(
        FakeLLMClient(evaluation_response("CONCLUDE"))
    ).evaluate(state)

    assert contract["conclude_allowed"] is True
    assert "CONCLUDE" in contract["allowed_decisions"]
    assert decision.value == "CONCLUDE"


def test_supported_hypothesis_with_real_evidence_cannot_conclude_regardless_of_confidence() -> None:
    state, evidence, hypothesis = build_evaluation_state()
    state["hypotheses"] = [
        hypothesis.model_copy(
            update={
                "status": HypothesisStatus.SUPPORTED,
                "confidence": 0.9,
                "supporting_evidence_ids": [evidence.id],
            }
        )
    ]

    contract = _build_prompt_context(state)["decision_contract"]

    assert contract["allowed_decisions"] == ["CONTINUE", "NEEDS_MANUAL_ACTION"]
    assert contract["conclude_allowed"] is False
    assert contract["supported_is_insufficient_for_conclude"] is True


@pytest.mark.parametrize("status", [HypothesisStatus.ACTIVE, HypothesisStatus.REJECTED])
def test_non_confirmed_hypotheses_do_not_allow_conclude(status: HypothesisStatus) -> None:
    state, evidence, hypothesis = build_evaluation_state()
    state["hypotheses"] = [
        hypothesis.model_copy(
            update={"status": status, "supporting_evidence_ids": [evidence.id]}
        )
    ]

    contract = _build_prompt_context(state)["decision_contract"]

    assert "CONCLUDE" not in contract["allowed_decisions"]
    assert contract["conclude_allowed"] is False


def test_confirmed_hypothesis_without_supporting_evidence_does_not_allow_conclude() -> None:
    state, _, hypothesis = build_evaluation_state()
    state["hypotheses"] = [
        hypothesis.model_copy(update={"status": HypothesisStatus.CONFIRMED})
    ]

    contract = _build_prompt_context(state)["decision_contract"]

    assert "CONCLUDE" not in contract["allowed_decisions"]
    assert contract["conclude_allowed"] is False


def test_confirmed_hypothesis_with_unknown_evidence_does_not_allow_conclude() -> None:
    state, _, hypothesis = build_evaluation_state()
    state["hypotheses"] = [
        hypothesis.model_copy(
            update={
                "status": HypothesisStatus.CONFIRMED,
                "supporting_evidence_ids": [uuid4()],
            }
        )
    ]

    contract = _build_prompt_context(state)["decision_contract"]

    assert "CONCLUDE" not in contract["allowed_decisions"]
    assert contract["conclude_allowed"] is False


def test_conclusion_without_confirmed_hypothesis_is_rejected_without_state_changes() -> None:
    state, evidence, hypothesis = build_evaluation_state()
    evidence_before = [*state["evidence"]]
    history_before = [*state["tool_history"]]

    with pytest.raises(EvidenceEvaluationError, match="CONFIRMED hypothesis"):
        LLMEvidenceEvaluator(FakeLLMClient(evaluation_response("CONCLUDE"))).evaluate(state)

    assert state["hypotheses"] == [hypothesis]
    assert state["evidence"] == evidence_before
    assert state["tool_history"] == history_before
    assert state["tool_call_count"] == 0
    assert state["investigation_round"] == 0


def test_confirmed_hypothesis_with_unknown_evidence_is_rejected() -> None:
    state, _, hypothesis = build_evaluation_state()
    state["hypotheses"] = [
        hypothesis.model_copy(
            update={
                "status": HypothesisStatus.CONFIRMED,
                "supporting_evidence_ids": [uuid4()],
            }
        )
    ]

    with pytest.raises(EvidenceEvaluationError, match="real supporting evidence"):
        LLMEvidenceEvaluator(FakeLLMClient(evaluation_response("CONCLUDE"))).evaluate(state)


def test_needs_manual_action_and_failures_do_not_invent_a_decision() -> None:
    state, evidence, hypothesis = build_evaluation_state()
    evaluator = LLMEvidenceEvaluator(FakeLLMClient(evaluation_response("NEEDS_MANUAL_ACTION")))

    assert evaluator.evaluate(state).value == "NEEDS_MANUAL_ACTION"
    with pytest.raises(EvidenceEvaluationError, match="output validation failed"):
        LLMEvidenceEvaluator(FakeLLMClient("not JSON")).evaluate(state)
    with pytest.raises(EvidenceEvaluationError, match="provider failed"):
        LLMEvidenceEvaluator(FakeLLMClient(LLMError("network unavailable"))).evaluate(state)
    assert state["hypotheses"] == [hypothesis]
    assert state["evidence"] == [evidence]
    assert state["tool_history"]
    assert state["evaluation_decision"] is None


def test_real_evaluator_works_with_existing_workflow_evaluation_contract() -> None:
    state, _, _ = build_evaluation_state()

    updated = evidence_evaluation_node(
        state,
        LLMEvidenceEvaluator(FakeLLMClient(evaluation_response("CONTINUE"))),
        InvestigationLoopLimits(),
    )

    assert updated["evaluation_decision"].value == "CONTINUE"
    assert updated["current_stage"] is AgentStage.INVESTIGATION_PLANNING
    assert updated["terminal_reason"] is None


def test_needs_manual_evaluation_sets_a_stable_inconclusive_terminal_reason() -> None:
    state, _, _ = build_evaluation_state()

    updated = evidence_evaluation_node(
        state,
        LLMEvidenceEvaluator(FakeLLMClient(evaluation_response("NEEDS_MANUAL_ACTION"))),
        InvestigationLoopLimits(),
    )

    assert updated["evaluation_decision"] is EvaluationDecision.NEEDS_MANUAL_ACTION
    assert updated["terminal_reason"] is TerminalReason.INVESTIGATION_INCONCLUSIVE


def test_grounded_confirmed_hypothesis_concludes_without_calling_evaluator() -> None:
    state, evidence, hypothesis = build_evaluation_state()
    state["hypotheses"] = [
        hypothesis.model_copy(
            update={
                "status": HypothesisStatus.CONFIRMED,
                "supporting_evidence_ids": [evidence.id],
            }
        )
    ]
    state["llm_call_count"] = 4
    evaluator = RecordingEvaluator(EvaluationDecision.NEEDS_MANUAL_ACTION)

    updated = evidence_evaluation_node(state, evaluator, InvestigationLoopLimits())

    assert is_conclusion_eligible(state) is True
    assert evaluator.calls == 0
    assert updated["evaluation_decision"] is EvaluationDecision.CONCLUDE
    assert updated["llm_call_count"] == 4
    assert updated["terminal_reason"] is None


@pytest.mark.parametrize("case", ["missing_support", "unknown_support", "supported"])
def test_non_grounded_states_still_delegate_to_the_evaluator(case: str) -> None:
    state, evidence, hypothesis = build_evaluation_state()
    if case == "missing_support":
        state["hypotheses"] = [
            hypothesis.model_copy(update={"status": HypothesisStatus.CONFIRMED})
        ]
    elif case == "unknown_support":
        state["hypotheses"] = [
            hypothesis.model_copy(
                update={
                    "status": HypothesisStatus.CONFIRMED,
                    "supporting_evidence_ids": [uuid4()],
                }
            )
        ]
    else:
        state["hypotheses"] = [
            hypothesis.model_copy(
                update={
                    "status": HypothesisStatus.SUPPORTED,
                    "confidence": 1.0,
                    "supporting_evidence_ids": [evidence.id],
                }
            )
        ]
    evaluator = RecordingEvaluator(EvaluationDecision.CONTINUE)

    updated = evidence_evaluation_node(state, evaluator, InvestigationLoopLimits())

    assert is_conclusion_eligible(state) is False
    assert evaluator.calls == 1
    assert updated["evaluation_decision"] is EvaluationDecision.CONTINUE


def test_any_grounded_confirmed_hypothesis_enables_deterministic_conclusion() -> None:
    state, evidence, hypothesis = build_evaluation_state()
    state["hypotheses"] = [
        hypothesis,
        hypothesis.model_copy(
            update={
                "id": uuid4(),
                "status": HypothesisStatus.CONFIRMED,
                "supporting_evidence_ids": [evidence.id],
            }
        ),
    ]
    evaluator = RecordingEvaluator(EvaluationDecision.CONTINUE)

    updated = evidence_evaluation_node(state, evaluator, InvestigationLoopLimits())

    assert updated["evaluation_decision"] is EvaluationDecision.CONCLUDE
    assert evaluator.calls == 0
