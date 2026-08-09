"""LangGraph wiring tests for the bounded Task 3.8 investigation loop."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import devsupport_backend.agent.nodes.tool_execution as execution_module
import devsupport_backend.agent.workflow as workflow_module
from devsupport_backend.agent.nodes.tool_execution import ToolExecutionDependencies
from devsupport_backend.agent.state import (
    AgentStage,
    AgentState,
    EvaluationDecision,
    EvidenceContext,
    create_initial_agent_state,
)
from devsupport_backend.agent.workflow import (
    InvestigationLoopLimits,
    InvestigationWorkflowDependencies,
    build_investigation_graph,
)
from devsupport_backend.models import Incident
from devsupport_backend.tools.schemas import (
    MetricSnapshot,
    QueryMetricsOutput,
    ToolError,
    ToolStatus,
)


class WorkflowFakeLLM:
    """Produce context-derived structured outputs for existing LLM node boundaries."""

    def __init__(self) -> None:
        self.generation_calls = 0
        self.planning_calls = 0
        self.update_calls = 0
        self.resolution_calls = 0

    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        context = json.loads(user_prompt)
        if system_prompt.startswith("You generate candidate"):
            self.generation_calls += 1
            return json.dumps(
                {
                    "hypotheses": [
                        {
                            "summary": "A service-local condition may affect requests.",
                            "confidence": 0.5,
                            "supporting_evidence_ids": [],
                            "next_check": "Inspect one runtime signal.",
                        },
                        {
                            "summary": "A dependency signal may affect requests.",
                            "confidence": 0.4,
                            "supporting_evidence_ids": [],
                            "next_check": "Inspect dependency-related evidence.",
                        },
                    ]
                }
            )
        if system_prompt.startswith("Plan exactly"):
            self.planning_calls += 1
            incident = context["incident"]
            return json.dumps(
                {
                    "investigation_goal": "Collect one metric snapshot for the current incident.",
                    "tool_name": "query_metrics",
                    "tool_arguments": {
                        "service": incident["service"],
                        "environment": incident["environment"],
                    },
                    "reason": "Metrics can distinguish the active candidate explanations.",
                }
            )
        if system_prompt.startswith("Update only"):
            self.update_calls += 1
            evidence_id = context["evidence"][-1]["id"]
            return json.dumps(
                {
                    "updates": [
                        {
                            "hypothesis_id": hypothesis["id"],
                            "supporting_evidence_ids": [evidence_id],
                            "contradicting_evidence_ids": [],
                            "confidence": 0.6,
                            "status": "CONFIRMED" if index == 0 else "SUPPORTED",
                            "next_check": "Collect another distinguishing signal if needed.",
                        }
                        for index, hypothesis in enumerate(context["hypotheses"])
                    ]
                }
            )
        if system_prompt.startswith("Generate one structured"):
            self.resolution_calls += 1
            confirmed = next(
                hypothesis
                for hypothesis in context["hypotheses"]
                if hypothesis["status"] == "CONFIRMED"
            )
            return json.dumps(
                {
                    "confirmed_hypothesis_id": confirmed["id"],
                    "root_cause": confirmed["summary"],
                    "confidence": confirmed["confidence"],
                    "recommended_action": "Request operator review of the confirmed evidence.",
                    "action_type": "manual_remediation",
                    "reason": "The confirmed hypothesis is tied to the cited evidence.",
                    "supporting_evidence_ids": confirmed["supporting_evidence_ids"],
                    "risk": "Any change requires later policy review and human approval.",
                }
            )
        raise AssertionError("unexpected LLM node prompt")


class FakeEvaluator:
    """Task 3.8 evaluation contract fake with explicit decision ordering."""

    def __init__(self, decisions: list[EvaluationDecision]) -> None:
        self._decisions = decisions
        self.calls = 0

    def evaluate(self, state: AgentState) -> EvaluationDecision:
        self.calls += 1
        assert state["current_stage"] is AgentStage.EVIDENCE_EVALUATION
        return self._decisions.pop(0)


def _build_initial_state() -> AgentState:
    started_at = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
    incident = Incident(
        id=uuid4(),
        service="catalog-service",
        environment="staging",
        description="The catalog endpoint returns errors after a recent change.",
        time_range_start=started_at,
        time_range_end=started_at + timedelta(minutes=5),
    )
    return create_initial_agent_state(incident, symptoms=["Catalog endpoint returns errors"])


def _fake_retrieval(state: AgentState, _rag_service: object) -> AgentState:
    """Replace database retrieval only in graph wiring tests."""
    knowledge_evidence = EvidenceContext(
        evidence_type="knowledge_retrieval",
        source="search_knowledge",
        summary="A concise runbook reference is available for investigation context.",
        reference="knowledge/runbooks/catalog-errors.md#checks",
    )
    return {
        **state,
        "evidence": [*state["evidence"], knowledge_evidence],
        "current_stage": AgentStage.HYPOTHESIS_GENERATION,
    }


def _successful_metrics_output() -> QueryMetricsOutput:
    return QueryMetricsOutput(
        status=ToolStatus.SUCCESS,
        duration_ms=2.0,
        metrics=MetricSnapshot(
            service="catalog-service",
            environment="staging",
            health_status="ok",
            request_count=5,
            success_count=4,
            error_count=1,
            error_rate=0.2,
            last_request_duration_ms=15.0,
            average_request_duration_ms=10.0,
        ),
    )


def _failed_metrics_output() -> QueryMetricsOutput:
    return QueryMetricsOutput(
        status=ToolStatus.UNAVAILABLE,
        duration_ms=2.0,
        error=ToolError(code="adapter_unavailable", message="metrics adapter unavailable"),
    )


def _workflow_dependencies(
    llm_client: WorkflowFakeLLM,
    evaluator: FakeEvaluator,
) -> InvestigationWorkflowDependencies:
    tool_execution = ToolExecutionDependencies(  # type: ignore[arg-type]
        rag_service=object(),
        logs_adapter=object(),
        metrics_adapter=object(),
        traces_adapter=object(),
        deployment_adapter=object(),
    )
    return InvestigationWorkflowDependencies(  # type: ignore[arg-type]
        rag_service=object(),
        llm_client=llm_client,
        tool_execution=tool_execution,
        evaluator=evaluator,
    )


def _run_workflow(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tool_outputs: list[QueryMetricsOutput],
    evaluator: FakeEvaluator,
    limits: InvestigationLoopLimits | None = None,
) -> tuple[dict[str, object], WorkflowFakeLLM, tuple[int, int]]:
    retrieval_calls = 0
    tool_calls = 0

    def fake_retrieval(state: AgentState, rag_service: object) -> AgentState:
        nonlocal retrieval_calls
        retrieval_calls += 1
        return _fake_retrieval(state, rag_service)

    def fake_query_metrics(*_: object) -> QueryMetricsOutput:
        nonlocal tool_calls
        tool_calls += 1
        return tool_outputs.pop(0)

    monkeypatch.setattr(workflow_module, "retrieval_node", fake_retrieval)
    monkeypatch.setattr(execution_module, "query_metrics", fake_query_metrics)
    llm_client = WorkflowFakeLLM()
    graph = build_investigation_graph(
        _workflow_dependencies(llm_client, evaluator),
        limits=limits,
    )
    return graph.invoke(_build_initial_state()), llm_client, (retrieval_calls, tool_calls)


def test_graph_compiles() -> None:
    evaluator = FakeEvaluator([EvaluationDecision.CONCLUDE])

    graph = build_investigation_graph(_workflow_dependencies(WorkflowFakeLLM(), evaluator))

    assert "evidence_evaluation" in graph.get_graph().nodes
    assert "tool_execution" in graph.get_graph().nodes


def test_graph_compiles_with_an_injected_checkpointer() -> None:
    evaluator = FakeEvaluator([EvaluationDecision.CONCLUDE])

    graph = build_investigation_graph(
        _workflow_dependencies(WorkflowFakeLLM(), evaluator),
        checkpointer=InMemorySaver(),
    )

    assert "evidence_evaluation" in graph.get_graph().nodes


def test_continue_forms_a_second_plan_then_conclude_without_repeating_initial_nodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluator = FakeEvaluator([EvaluationDecision.CONTINUE, EvaluationDecision.CONCLUDE])

    result, llm_client, calls = _run_workflow(
        monkeypatch,
        tool_outputs=[_successful_metrics_output(), _successful_metrics_output()],
        evaluator=evaluator,
    )

    assert result["evaluation_decision"] is EvaluationDecision.CONCLUDE
    assert result["current_stage"] is AgentStage.CONCLUSION
    assert result["final_conclusion"] is not None
    assert result["proposed_action"] is not None
    assert result["investigation_round"] == 2
    assert result["tool_call_count"] == 2
    assert llm_client.generation_calls == 1
    assert llm_client.planning_calls == 2
    assert llm_client.update_calls == 2
    assert llm_client.resolution_calls == 1
    assert evaluator.calls == 2
    assert calls == (1, 2)


def test_tool_failure_replans_without_incrementing_the_failed_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluator = FakeEvaluator([EvaluationDecision.CONCLUDE])

    result, llm_client, _ = _run_workflow(
        monkeypatch,
        tool_outputs=[_failed_metrics_output(), _successful_metrics_output()],
        evaluator=evaluator,
    )

    assert result["evaluation_decision"] is EvaluationDecision.CONCLUDE
    assert result["tool_call_count"] == 2
    assert result["investigation_round"] == 1
    assert llm_client.planning_calls == 2
    assert llm_client.update_calls == 1
    assert llm_client.resolution_calls == 1
    assert evaluator.calls == 1


@pytest.mark.parametrize(
    ("limits", "limit_field"),
    [
        (InvestigationLoopLimits(max_rounds=1, max_tool_calls=5), "investigation_round"),
        (InvestigationLoopLimits(max_rounds=5, max_tool_calls=1), "tool_call_count"),
    ],
)
def test_limit_stops_future_planning_and_tools_with_manual_action(
    monkeypatch: pytest.MonkeyPatch,
    limits: InvestigationLoopLimits,
    limit_field: str,
) -> None:
    evaluator = FakeEvaluator([EvaluationDecision.CONTINUE])

    result, llm_client, calls = _run_workflow(
        monkeypatch,
        tool_outputs=[_successful_metrics_output()],
        evaluator=evaluator,
        limits=limits,
    )

    assert result["evaluation_decision"] is EvaluationDecision.NEEDS_MANUAL_ACTION
    assert result[limit_field] == 1
    assert result["investigation_round"] == 1
    assert result["tool_call_count"] == 1
    assert llm_client.planning_calls == 1
    assert llm_client.update_calls == 1
    assert llm_client.resolution_calls == 0
    assert evaluator.calls == 1
    assert calls == (1, 1)


def test_final_allowed_round_can_conclude_and_propose_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluator = FakeEvaluator([EvaluationDecision.CONCLUDE])

    result, llm_client, calls = _run_workflow(
        monkeypatch,
        tool_outputs=[_successful_metrics_output()],
        evaluator=evaluator,
        limits=InvestigationLoopLimits(max_rounds=1, max_tool_calls=5),
    )

    assert result["evaluation_decision"] is EvaluationDecision.CONCLUDE
    assert result["current_stage"] is AgentStage.CONCLUSION
    assert result["final_conclusion"] is not None
    assert result["proposed_action"] is not None
    assert result["investigation_round"] == 1
    assert result["tool_call_count"] == 1
    assert llm_client.planning_calls == 1
    assert llm_client.update_calls == 1
    assert llm_client.resolution_calls == 1
    assert evaluator.calls == 1
    assert calls == (1, 1)
