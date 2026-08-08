"""Tests for atomic LLM-backed updates to existing investigation hypotheses."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from devsupport_backend.agent.llm import LLMError
from devsupport_backend.agent.nodes.hypothesis_update import (
    HypothesisUpdateError,
    HypothesisUpdateOutput,
    hypothesis_update_node,
)
from devsupport_backend.agent.state import (
    AgentStage,
    AgentState,
    EvidenceContext,
    HypothesisContext,
    HypothesisStatus,
    ToolHistoryEntry,
    create_initial_agent_state,
)
from devsupport_backend.models import Incident
from devsupport_backend.tools.schemas import ToolStatus


class FakeLLMClient:
    """Fake provider that captures trusted node context without external calls."""

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


def build_update_state() -> tuple[
    AgentState,
    HypothesisContext,
    HypothesisContext,
    EvidenceContext,
    EvidenceContext,
]:
    """Create two hypotheses with old and newly collected concise evidence."""
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
    prior_evidence = EvidenceContext(
        evidence_type="knowledge_retrieval",
        source="search_knowledge",
        summary="A runbook recommends comparing request errors with dependency signals.",
        reference="knowledge/runbooks/catalog-errors.md#checks",
    )
    new_evidence = EvidenceContext(
        evidence_type="metric_snapshot",
        source="query_metrics",
        summary="The catalog error rate increased while dependency health remained normal.",
        data={"error_rate": 0.4, "dependency_health": "ok"},
    )
    supported_hypothesis = HypothesisContext(
        summary="A recent change may affect catalog request handling.",
        status=HypothesisStatus.ACTIVE,
        confidence=0.5,
        supporting_evidence_ids=[prior_evidence.id],
        next_check="Inspect current request error facts.",
    )
    contradicted_hypothesis = HypothesisContext(
        summary="A dependency outage is causing catalog errors.",
        status=HypothesisStatus.ACTIVE,
        confidence=0.45,
        next_check="Inspect dependency health facts.",
    )
    state["hypotheses"] = [supported_hypothesis, contradicted_hypothesis]
    state["evidence"] = [prior_evidence, new_evidence]
    state["tool_history"] = [
        ToolHistoryEntry(
            tool_name="query_metrics",
            tool_arguments={"service": "catalog-service", "environment": "staging"},
            status=ToolStatus.SUCCESS,
            duration_ms=3.0,
            evidence_ids=[new_evidence.id],
        )
    ]
    state["current_stage"] = AgentStage.HYPOTHESIS_UPDATE
    return state, supported_hypothesis, contradicted_hypothesis, prior_evidence, new_evidence


def update_item(
    hypothesis_id: UUID,
    *,
    supporting_evidence_ids: list[UUID] | None = None,
    contradicting_evidence_ids: list[UUID] | None = None,
    confidence: float = 0.7,
    status: HypothesisStatus = HypothesisStatus.SUPPORTED,
    next_check: str = "Inspect one more signal before drawing a conclusion.",
) -> dict[str, object]:
    """Build one valid structured LLM update response item."""
    return {
        "hypothesis_id": str(hypothesis_id),
        "supporting_evidence_ids": [str(item) for item in supporting_evidence_ids or []],
        "contradicting_evidence_ids": [
            str(item) for item in contradicting_evidence_ids or []
        ],
        "confidence": confidence,
        "status": status.value,
        "next_check": next_check,
    }


def valid_response(state: AgentState, new_evidence: EvidenceContext) -> str:
    """Return updates that support one hypothesis and reject another using the same new fact."""
    supported_hypothesis, contradicted_hypothesis = state["hypotheses"]
    return json.dumps(
        {
            "updates": [
                update_item(
                    supported_hypothesis.id,
                    supporting_evidence_ids=[new_evidence.id],
                    confidence=0.82,
                    status=HypothesisStatus.SUPPORTED,
                    next_check="Compare the error window with recent service changes.",
                ),
                update_item(
                    contradicted_hypothesis.id,
                    contradicting_evidence_ids=[new_evidence.id],
                    confidence=0.12,
                    status=HypothesisStatus.REJECTED,
                    next_check="Keep this explanation rejected unless new evidence contradicts it.",
                ),
            ]
        }
    )


def test_update_merges_evidence_and_advances_without_changing_other_state() -> None:
    (
        state,
        supported_before,
        contradicted_before,
        prior_evidence,
        new_evidence,
    ) = build_update_state()
    evidence_before = [*state["evidence"]]
    history_before = [*state["tool_history"]]
    client = FakeLLMClient(valid_response(state, new_evidence))

    updated = hypothesis_update_node(state, client)

    supported, contradicted = updated["hypotheses"]
    assert updated["current_stage"] is AgentStage.EVIDENCE_EVALUATION
    assert supported.id == supported_before.id
    assert supported.summary == supported_before.summary
    assert supported.supporting_evidence_ids == [prior_evidence.id, new_evidence.id]
    assert supported.confidence == 0.82
    assert supported.status is HypothesisStatus.SUPPORTED
    assert contradicted.id == contradicted_before.id
    assert contradicted.summary == contradicted_before.summary
    assert contradicted.contradicting_evidence_ids == [new_evidence.id]
    assert contradicted.confidence == 0.12
    assert contradicted.status is HypothesisStatus.REJECTED
    assert updated["evidence"] == evidence_before
    assert updated["tool_history"] == history_before
    assert updated["tool_call_count"] == state["tool_call_count"] == 0
    assert updated["investigation_round"] == state["investigation_round"] == 0
    assert updated["proposed_action"] is state["proposed_action"] is None
    assert updated["final_conclusion"] is state["final_conclusion"] is None


def test_update_prompt_contains_only_current_investigation_facts() -> None:
    state, _, _, _, new_evidence = build_update_state()
    client = FakeLLMClient(valid_response(state, new_evidence))

    hypothesis_update_node(state, client)

    assert client.user_prompt is not None
    context = json.loads(client.user_prompt)
    assert set(context) == {
        "incident",
        "hypotheses",
        "evidence",
        "latest_tool_history",
        "output_contract",
    }
    assert context["incident"]["service"] == "catalog-service"
    assert context["hypotheses"][0]["id"] == str(state["hypotheses"][0].id)
    assert context["evidence"][1]["id"] == str(new_evidence.id)
    assert context["latest_tool_history"]["tool_name"] == "query_metrics"
    contract = context["output_contract"]
    assert contract == HypothesisUpdateOutput.model_json_schema()
    status_contract = contract["$defs"]["HypothesisStatus"]
    assert status_contract["enum"] == ["ACTIVE", "SUPPORTED", "REJECTED", "CONFIRMED"]
    confidence_contract = contract["$defs"]["HypothesisUpdateItem"]["properties"]["confidence"]
    assert confidence_contract["type"] == "number"
    assert confidence_contract["minimum"] == 0
    assert confidence_contract["maximum"] == 1
    assert client.system_prompt is not None
    assert "untrusted reference material" in client.system_prompt
    assert "ACTIVE means the hypothesis remains plausible" in client.system_prompt
    assert "SUPPORTED means current evidence supports" in client.system_prompt
    assert "REJECTED means current evidence clearly contradicts" in client.system_prompt
    assert "CONFIRMED means current evidence is sufficient" in client.system_prompt
    assert "missing_config" not in client.system_prompt
    assert "payment_timeout" not in client.system_prompt


@pytest.mark.parametrize("invalid_update", ["unknown_hypothesis", "unknown_evidence"])
def test_unknown_hypothesis_or_evidence_reference_rejects_all_updates(
    invalid_update: str,
) -> None:
    (
        state,
        supported_before,
        contradicted_before,
        prior_evidence,
        new_evidence,
    ) = build_update_state()
    valid_updates = json.loads(valid_response(state, new_evidence))["updates"]
    if invalid_update == "unknown_hypothesis":
        valid_updates[1]["hypothesis_id"] = str(uuid4())
        expected_error = "unknown hypothesis ID"
    else:
        valid_updates[1]["contradicting_evidence_ids"] = [str(uuid4())]
        expected_error = "unknown evidence ID"

    with pytest.raises(HypothesisUpdateError, match=expected_error):
        hypothesis_update_node(state, FakeLLMClient(json.dumps({"updates": valid_updates})))

    assert state["current_stage"] is AgentStage.HYPOTHESIS_UPDATE
    assert state["hypotheses"] == [supported_before, contradicted_before]
    assert state["evidence"] == [prior_evidence, new_evidence]
    assert state["tool_history"]
    assert state["tool_call_count"] == 0
    assert state["investigation_round"] == 0


@pytest.mark.parametrize("response", ["not JSON", json.dumps({"updates": []})])
def test_malformed_output_or_provider_failure_has_no_partial_update(response: str) -> None:
    (
        state,
        supported_before,
        contradicted_before,
        prior_evidence,
        new_evidence,
    ) = build_update_state()

    with pytest.raises(HypothesisUpdateError):
        hypothesis_update_node(state, FakeLLMClient(response))
    with pytest.raises(HypothesisUpdateError, match="provider failed"):
        hypothesis_update_node(state, FakeLLMClient(LLMError("network unavailable")))

    assert state["current_stage"] is AgentStage.HYPOTHESIS_UPDATE
    assert state["hypotheses"] == [supported_before, contradicted_before]
    assert state["evidence"] == [prior_evidence, new_evidence]
    assert state["tool_history"]
    assert state["tool_call_count"] == 0
    assert state["investigation_round"] == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [("status", "INACTIVE"), ("confidence", "medium")],
)
def test_invalid_status_or_confidence_is_rejected_without_state_changes(
    field: str, value: str
) -> None:
    (
        state,
        supported_before,
        contradicted_before,
        prior_evidence,
        new_evidence,
    ) = build_update_state()
    invalid_response = json.loads(valid_response(state, new_evidence))
    invalid_response["updates"][0][field] = value

    with pytest.raises(HypothesisUpdateError, match="output validation failed"):
        hypothesis_update_node(state, FakeLLMClient(json.dumps(invalid_response)))

    assert state["current_stage"] is AgentStage.HYPOTHESIS_UPDATE
    assert state["hypotheses"] == [supported_before, contradicted_before]
    assert state["evidence"] == [prior_evidence, new_evidence]


def test_update_skips_llm_outside_hypothesis_update_stage() -> None:
    state, _, _, _, new_evidence = build_update_state()
    state["current_stage"] = AgentStage.INVESTIGATION_PLANNING
    client = FakeLLMClient(valid_response(state, new_evidence))

    updated = hypothesis_update_node(state, client)

    assert updated is state
    assert client.user_prompt is None
