"""Tests for the LLM planner's safe, schema-validated Tool plans."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from devsupport_backend.agent.llm import LLMError
from devsupport_backend.agent.nodes.planner import PlanningError, investigation_planner_node
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
from devsupport_backend.tools.registry import ToolName, tool_registry
from devsupport_backend.tools.schemas import ToolStatus


class FakeLLMClient:
    """Fake provider capturing the exact Planner context without network access."""

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


def build_planning_state() -> tuple[AgentState, EvidenceContext, HypothesisContext]:
    """Create a minimal, non-scenario-specific investigation planning state."""
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
        evidence_type="knowledge_retrieval",
        source="search_knowledge",
        summary="A runbook describes checking request errors and dependency signals.",
        data={"citation": {"id": "catalog-runbook#checks"}},
        reference="knowledge/runbooks/catalog-errors.md#checks",
    )
    hypothesis = HypothesisContext(
        summary="A recent change may affect catalog request handling.",
        status=HypothesisStatus.ACTIVE,
        confidence=0.6,
        supporting_evidence_ids=[evidence.id],
        next_check="Inspect request errors during the incident window.",
    )
    state["evidence"] = [evidence]
    state["hypotheses"] = [hypothesis]
    state["tool_history"] = [
        ToolHistoryEntry(
            tool_name="search_knowledge",
            tool_arguments={"query": "catalog errors"},
            status=ToolStatus.SUCCESS,
            duration_ms=4.0,
            evidence_ids=[evidence.id],
        )
    ]
    state["current_stage"] = AgentStage.INVESTIGATION_PLANNING
    return state, evidence, hypothesis


def plan_response(*, tool_name: str, arguments: dict[str, object]) -> str:
    """Build one structured LLM planning response."""
    return json.dumps(
        {
            "investigation_goal": (
                "Check the next signal that can distinguish the active hypotheses."
            ),
            "tool_name": tool_name,
            "tool_arguments": arguments,
            "reason": "This check is relevant to the current evidence.",
        }
    )


def test_planner_creates_valid_pending_tool_call_and_advances_stage() -> None:
    state, evidence, hypothesis = build_planning_state()
    response = plan_response(
        tool_name="query_logs",
        arguments={
            "service": "catalog-service",
            "environment": "staging",
            "time_range_start": "2026-08-08T10:00:00+00:00",
            "time_range_end": "2026-08-08T10:05:00+00:00",
            "limit": 10,
        },
    )

    updated = investigation_planner_node(state, FakeLLMClient(response))

    assert updated["current_stage"] is AgentStage.TOOL_EXECUTION
    assert updated["current_goal"] == (
        "Check the next signal that can distinguish the active hypotheses."
    )
    assert updated["pending_tool_call"] is not None
    assert updated["pending_tool_call"].tool_name.value == "query_logs"
    assert updated["pending_tool_call"].tool_arguments["limit"] == 10
    assert updated["evidence"] == [evidence]
    assert updated["hypotheses"] == [hypothesis]
    assert updated["tool_history"] == state["tool_history"]
    assert updated["tool_call_count"] == state["tool_call_count"] == 0
    assert updated["investigation_round"] == state["investigation_round"] == 0
    assert updated["proposed_action"] is state["proposed_action"] is None
    assert updated["final_conclusion"] is state["final_conclusion"] is None


def test_planner_context_contains_current_investigation_facts() -> None:
    state, evidence, hypothesis = build_planning_state()
    client = FakeLLMClient(
        plan_response(
            tool_name="search_knowledge",
            arguments={"query": "catalog request errors", "service": "catalog-service"},
        )
    )

    investigation_planner_node(state, client)

    assert client.user_prompt is not None
    context = json.loads(client.user_prompt)
    assert set(context) == {
        "incident",
        "hypotheses",
        "evidence",
        "tool_history",
        "tool_input_contracts",
    }
    assert context["incident"]["service"] == "catalog-service"
    assert context["hypotheses"][0]["id"] == str(hypothesis.id)
    assert context["evidence"][0]["id"] == str(evidence.id)
    assert context["tool_history"][0]["tool_name"] == "search_knowledge"
    contracts = context["tool_input_contracts"]
    assert set(contracts) == {
        "search_knowledge",
        "query_logs",
        "query_metrics",
        "query_traces",
        "get_deployment_history",
    }
    assert contracts["query_metrics"] == tool_registry.get(
        ToolName.QUERY_METRICS
    ).input_model.model_json_schema()
    assert set(contracts["query_metrics"]["properties"]) == {"service", "environment"}
    assert {"time_range_start", "time_range_end"}.issubset(
        contracts["query_logs"]["properties"]
    )
    assert {"time_range_start", "time_range_end"}.issubset(
        contracts["query_traces"]["properties"]
    )
    assert "time_range_start" not in contracts["get_deployment_history"]["properties"]
    assert "rollback_deployment" not in contracts
    assert client.system_prompt is not None
    assert "rollback_deployment" in client.system_prompt
    assert "Select exactly one Tool" in client.system_prompt
    assert "do not add fields that are absent" in client.system_prompt


def test_planner_can_choose_different_tools_from_different_valid_outputs() -> None:
    knowledge_state, _, _ = build_planning_state()
    metrics_state, metrics_evidence, metrics_hypothesis = build_planning_state()
    metrics_evidence.summary = "A metric snapshot indicates elevated error rate."
    metrics_hypothesis.summary = "A runtime signal needs validation."
    knowledge_plan = plan_response(
        tool_name="search_knowledge",
        arguments={"query": "catalog error runbook", "service": "catalog-service"},
    )
    metrics_plan = plan_response(
        tool_name="query_metrics",
        arguments={"service": "catalog-service", "environment": "staging"},
    )
    knowledge_client = FakeLLMClient(knowledge_plan)
    metrics_client = FakeLLMClient(metrics_plan)

    knowledge_updated = investigation_planner_node(knowledge_state, knowledge_client)
    metrics_updated = investigation_planner_node(metrics_state, metrics_client)

    assert knowledge_updated["pending_tool_call"] is not None
    assert metrics_updated["pending_tool_call"] is not None
    assert knowledge_updated["pending_tool_call"].tool_name.value == "search_knowledge"
    assert metrics_updated["pending_tool_call"].tool_name.value == "query_metrics"
    assert knowledge_client.user_prompt != metrics_client.user_prompt


@pytest.mark.parametrize(
    "tool_name,arguments,error_message",
    [
        ("run_shell", {}, "planner output validation failed"),
        (
            "rollback_deployment",
            {
                "service": "catalog-service",
                "environment": "staging",
                "target_version": "v1.0.0",
                "reason": "not permitted",
                "approval_id": str(uuid4()),
            },
            "disallowed tool",
        ),
        ("query_logs", {"service": "catalog-service"}, "tool arguments are invalid"),
        (
            "query_metrics",
            {
                "service": "catalog-service",
                "environment": "staging",
                "time_range_start": "2026-08-08T10:00:00+00:00",
                "time_range_end": "2026-08-08T10:05:00+00:00",
                "metrics": ["request_count"],
            },
            "tool arguments are invalid",
        ),
    ],
)
def test_planner_rejects_invalid_or_disallowed_tool_plans(
    tool_name: str, arguments: dict[str, object], error_message: str
) -> None:
    state, evidence, hypothesis = build_planning_state()
    history_before = [*state["tool_history"]]

    with pytest.raises(PlanningError, match=error_message):
        investigation_planner_node(
            state,
            FakeLLMClient(plan_response(tool_name=tool_name, arguments=arguments)),
        )

    assert state["pending_tool_call"] is None
    assert state["current_stage"] is AgentStage.INVESTIGATION_PLANNING
    assert state["evidence"] == [evidence]
    assert state["hypotheses"] == [hypothesis]
    assert state["tool_history"] == history_before
    assert state["tool_call_count"] == 0
    assert state["investigation_round"] == 0


@pytest.mark.parametrize("response", ["not JSON", json.dumps({})])
def test_malformed_output_or_provider_failure_does_not_create_plan(response: str) -> None:
    state, evidence, hypothesis = build_planning_state()
    history_before = [*state["tool_history"]]

    with pytest.raises(PlanningError):
        investigation_planner_node(state, FakeLLMClient(response))
    with pytest.raises(PlanningError, match="provider failed"):
        investigation_planner_node(state, FakeLLMClient(LLMError("network unavailable")))

    assert state["pending_tool_call"] is None
    assert state["current_goal"] is None
    assert state["evidence"] == [evidence]
    assert state["hypotheses"] == [hypothesis]
    assert state["tool_history"] == history_before
    assert state["tool_call_count"] == 0
    assert state["investigation_round"] == 0


def test_planner_skips_llm_outside_planning_stage() -> None:
    state, _, _ = build_planning_state()
    state["current_stage"] = AgentStage.HYPOTHESIS_GENERATION
    client = FakeLLMClient(
        plan_response(
            tool_name="search_knowledge",
            arguments={"query": "catalog errors"},
        )
    )

    updated = investigation_planner_node(state, client)

    assert updated is state
    assert client.user_prompt is None
