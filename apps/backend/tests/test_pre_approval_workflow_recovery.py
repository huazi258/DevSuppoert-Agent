"""Generic persisted failed-task inspection and same-thread continuation tests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypedDict
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

import devsupport_backend.agent.workflow as workflow_module
from devsupport_backend.agent.llm import LLMError
from devsupport_backend.agent.nodes.planner import PlanningError, investigation_planner_node
from devsupport_backend.agent.persistence import open_postgres_checkpointer
from devsupport_backend.agent.runtime import WorkflowService
from devsupport_backend.agent.state import (
    AgentStage,
    AgentState,
    EvaluationDecision,
    create_initial_agent_state,
)
from devsupport_backend.agent.workflow import InvestigationLoopLimits
from devsupport_backend.workflow_console import PostgresWorkflowRuntime


class RecoveryState(TypedDict):
    """Small deterministic state used to exercise runtime continuation semantics."""

    history: list[str]
    predecessor_calls: int
    terminal_reached: bool


def _recovery_graph(
    *,
    planner_should_fail: list[bool],
    predecessor_calls: list[int],
    planner_calls: list[int],
    retry_observations: list[dict[str, object]],
):
    def successful_predecessor(state: RecoveryState) -> dict[str, object]:
        predecessor_calls[0] += 1
        return {
            "history": [*state["history"], "successful_predecessor"],
            "predecessor_calls": state["predecessor_calls"] + 1,
        }

    def investigation_planning(state: RecoveryState) -> dict[str, object]:
        planner_calls[0] += 1
        if planner_should_fail[0]:
            raise RuntimeError(
                "provider payload Authorization: Bearer secret-token\n"
                "traceback: controlled planner failure"
            )
        retry_observations.append(dict(state))
        return {"history": [*state["history"], "investigation_planning"]}

    def terminal(state: RecoveryState) -> dict[str, object]:
        return {
            "history": [*state["history"], "terminal"],
            "terminal_reached": True,
        }

    graph = StateGraph(RecoveryState)
    graph.add_node("successful_predecessor", successful_predecessor)
    graph.add_node("investigation_planning", investigation_planning)
    graph.add_node("terminal", terminal)
    graph.add_edge(START, "successful_predecessor")
    graph.add_edge("successful_predecessor", "investigation_planning")
    graph.add_edge("investigation_planning", "terminal")
    graph.add_edge("terminal", END)
    return graph.compile(checkpointer=InMemorySaver())


def _initial_state() -> RecoveryState:
    return {"history": [], "predecessor_calls": 0, "terminal_reached": False}


def test_workflow_service_inspects_persisted_failed_task() -> None:
    planner_should_fail = [True]
    predecessor_calls = [0]
    planner_calls = [0]
    retry_observations: list[dict[str, object]] = []
    graph = _recovery_graph(
        planner_should_fail=planner_should_fail,
        predecessor_calls=predecessor_calls,
        planner_calls=planner_calls,
        retry_observations=retry_observations,
    )
    service = WorkflowService(graph)
    failed_thread_id = str(uuid4())

    with pytest.raises(RuntimeError, match="controlled planner failure"):
        graph.invoke(_initial_state(), WorkflowService.config_for(failed_thread_id))

    failure = service.get_failure(failed_thread_id)

    assert failure is not None
    assert failure.failed_node == "investigation_planning"
    assert failure.safe_error
    assert "secret-token" not in failure.safe_error
    assert "Authorization" not in failure.safe_error
    assert "traceback" not in failure.safe_error

    planner_should_fail[0] = False
    healthy_thread_id = str(uuid4())
    graph.invoke(_initial_state(), WorkflowService.config_for(healthy_thread_id))

    assert service.get_failure(healthy_thread_id) is None


def test_workflow_service_retries_failed_planner_on_same_thread_without_replaying_predecessors(
) -> None:
    planner_should_fail = [True]
    predecessor_calls = [0]
    planner_calls = [0]
    retry_observations: list[dict[str, object]] = []
    graph = _recovery_graph(
        planner_should_fail=planner_should_fail,
        predecessor_calls=predecessor_calls,
        planner_calls=planner_calls,
        retry_observations=retry_observations,
    )
    service = WorkflowService(graph)
    thread_id = str(uuid4())

    with pytest.raises(RuntimeError, match="controlled planner failure"):
        graph.invoke(_initial_state(), WorkflowService.config_for(thread_id))

    planner_should_fail[0] = False
    result = service.retry_failed_task(thread_id)

    assert predecessor_calls == [1]
    assert planner_calls == [2]
    assert retry_observations == [
        {"history": ["successful_predecessor"], "predecessor_calls": 1, "terminal_reached": False}
    ]
    assert result == {
        "history": ["successful_predecessor", "investigation_planning", "terminal"],
        "predecessor_calls": 1,
        "terminal_reached": True,
    }
    assert service.get_failure(thread_id) is None


def test_workflow_service_second_failed_retry_remains_inspectable() -> None:
    planner_should_fail = [True]
    predecessor_calls = [0]
    planner_calls = [0]
    retry_observations: list[dict[str, object]] = []
    graph = _recovery_graph(
        planner_should_fail=planner_should_fail,
        predecessor_calls=predecessor_calls,
        planner_calls=planner_calls,
        retry_observations=retry_observations,
    )
    service = WorkflowService(graph)
    thread_id = str(uuid4())

    with pytest.raises(RuntimeError, match="controlled planner failure"):
        graph.invoke(_initial_state(), WorkflowService.config_for(thread_id))
    with pytest.raises(RuntimeError, match="controlled planner failure"):
        service.retry_failed_task(thread_id)

    snapshot = graph.get_state(WorkflowService.config_for(thread_id))
    failure = service.get_failure(thread_id)

    assert predecessor_calls == [1]
    assert planner_calls == [2]
    assert retry_observations == []
    assert list(snapshot.next) == ["investigation_planning"]
    assert any(
        task.name == "investigation_planning" and task.error is not None for task in snapshot.tasks
    )
    assert failure is not None
    assert failure.failed_node == "investigation_planning"


@dataclass
class _RecoveryIncident:
    """Minimal Incident source for the durable AgentState retry regression."""

    id: object
    service: str = "synthetic-service"
    environment: str = "local"
    description: str = "durable planner retry regression"
    time_range_start: datetime = datetime(2026, 8, 11, tzinfo=UTC)
    time_range_end: datetime = datetime(2026, 8, 11, tzinfo=UTC)


class _SequencedPlannerLLM:
    """Return a controlled provider failure followed by one valid Planner response."""

    def __init__(self, *, failing_calls: frozenset[int] = frozenset({1})) -> None:
        self.calls = 0
        self._failing_calls = failing_calls

    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        del system_prompt, user_prompt
        self.calls += 1
        if self.calls in self._failing_calls:
            raise LLMError("controlled planner provider failure")
        return json.dumps(
            {
                "investigation_goal": "Inspect payment latency.",
                "tool_name": "query_metrics",
                "tool_arguments": {
                    "service": "synthetic-service",
                    "environment": "local",
                },
                "reason": "Correlate latency.",
            }
        )


def _durable_agent_state_retry_graph(
    *,
    checkpointer: object,
    planner_llm: _SequencedPlannerLLM,
    calls: dict[str, int],
):
    """Use the actual Planner node and routing branch around a persisted failure."""

    def planning_guard(state: AgentState) -> AgentState:
        calls["planning_guard"] += 1
        return workflow_module._planning_guard_node(state, InvestigationLoopLimits())

    def planning(state: AgentState) -> AgentState:
        calls["planning"] += 1
        return investigation_planner_node(state, planner_llm)

    def tool_execution(state: AgentState) -> AgentState:
        calls["tool_execution"] += 1
        return {**state, "current_stage": AgentStage.NEEDS_MANUAL_ACTION}

    graph = StateGraph(AgentState)
    graph.add_node("planning_guard", planning_guard)
    graph.add_node("investigation_planning", planning)
    graph.add_node("tool_execution", tool_execution)
    graph.add_edge(START, "planning_guard")
    graph.add_conditional_edges(
        "planning_guard",
        lambda state: workflow_module._route_after_planning_guard(state, False),
        {"investigation_planning": "investigation_planning", "end": END},
    )
    graph.add_conditional_edges(
        "investigation_planning",
        workflow_module._route_after_planning,
        {"tool_execution": "tool_execution", "end": END},
    )
    graph.add_edge("tool_execution", END)
    return graph.compile(checkpointer=checkpointer)


def test_postgres_retry_usage_update_preserves_failed_planner_continuation(
    database_session,
) -> None:
    """A restored string stage must run the actual Planner and route to Tool Executor."""

    thread_id = str(uuid4())
    config = WorkflowService.config_for(thread_id)
    planner_llm = _SequencedPlannerLLM()
    calls = {"planning_guard": 0, "planning": 0, "tool_execution": 0}
    state = create_initial_agent_state(_RecoveryIncident(id=uuid4()))
    state["current_stage"] = AgentStage.INVESTIGATION_PLANNING
    state["active_execution_seconds"] = 70.0
    del state["workflow_retry_count"]

    try:
        with open_postgres_checkpointer() as first_checkpointer:
            first_graph = _durable_agent_state_retry_graph(
                checkpointer=first_checkpointer,
                planner_llm=planner_llm,
                calls=calls,
            )
            with pytest.raises(PlanningError, match="planner provider failed"):
                first_graph.invoke(state, config)

            failed_snapshot = first_graph.get_state(config)
            assert type(failed_snapshot.values["current_stage"]) is str
            assert failed_snapshot.values["current_stage"] == "investigation_planning"
            assert list(failed_snapshot.next) == ["investigation_planning"]
            assert any(
                task.name == "investigation_planning" and task.error is not None
                for task in failed_snapshot.tasks
            )

        runtime = PostgresWorkflowRuntime(database_session)
        runtime.record_retry_attempt(thread_id)

        recorded_state = runtime.get_state(thread_id)
        recorded_failure = runtime.get_failure(thread_id)
        assert recorded_state is not None
        assert recorded_state["workflow_retry_count"] == 1
        assert recorded_state["active_execution_seconds"] == 70.0
        assert recorded_failure is not None
        assert recorded_failure.failed_node == "investigation_planning"
        assert calls == {"planning_guard": 1, "planning": 1, "tool_execution": 0}

        with open_postgres_checkpointer() as second_checkpointer:
            second_graph = _durable_agent_state_retry_graph(
                checkpointer=second_checkpointer,
                planner_llm=planner_llm,
                calls=calls,
            )
            result = WorkflowService(second_graph).retry_failed_task(thread_id)

            assert calls == {"planning_guard": 1, "planning": 2, "tool_execution": 1}
            assert planner_llm.calls == 2
            assert result["current_stage"] == AgentStage.NEEDS_MANUAL_ACTION
            assert result["current_stage"] != AgentStage.INVESTIGATION_PLANNING
            assert result["workflow_retry_count"] == 1
            assert result["active_execution_seconds"] == 70.0
            assert list(second_graph.get_state(config).next) == []
    finally:
        with open_postgres_checkpointer() as checkpointer:
            checkpointer.delete_thread(thread_id)


def test_postgres_retry_usage_survives_a_second_failed_retry(
    database_session,
) -> None:
    """Every recorded retry survives failure and leaves the same task continuable."""

    thread_id = str(uuid4())
    config = WorkflowService.config_for(thread_id)
    planner_llm = _SequencedPlannerLLM(failing_calls=frozenset({1, 2}))
    calls = {"planning_guard": 0, "planning": 0, "tool_execution": 0}
    state = create_initial_agent_state(_RecoveryIncident(id=uuid4()))
    state["current_stage"] = AgentStage.INVESTIGATION_PLANNING
    runtime = PostgresWorkflowRuntime(database_session)

    try:
        with open_postgres_checkpointer() as first_checkpointer:
            first_graph = _durable_agent_state_retry_graph(
                checkpointer=first_checkpointer,
                planner_llm=planner_llm,
                calls=calls,
            )
            with pytest.raises(PlanningError, match="planner provider failed"):
                first_graph.invoke(state, config)

        runtime.record_retry_attempt(thread_id)
        with open_postgres_checkpointer() as second_checkpointer:
            second_graph = _durable_agent_state_retry_graph(
                checkpointer=second_checkpointer,
                planner_llm=planner_llm,
                calls=calls,
            )
            with pytest.raises(PlanningError, match="planner provider failed"):
                WorkflowService(second_graph).retry_failed_task(thread_id)

        failed_state = runtime.get_state(thread_id)
        failed_retry = runtime.get_failure(thread_id)
        assert failed_state is not None
        assert failed_state["workflow_retry_count"] == 1
        assert failed_retry is not None
        assert failed_retry.failed_node == "investigation_planning"
        assert calls == {"planning_guard": 1, "planning": 2, "tool_execution": 0}

        runtime.record_retry_attempt(thread_id)
        with open_postgres_checkpointer() as third_checkpointer:
            third_graph = _durable_agent_state_retry_graph(
                checkpointer=third_checkpointer,
                planner_llm=planner_llm,
                calls=calls,
            )
            result = WorkflowService(third_graph).retry_failed_task(thread_id)

            assert result["workflow_retry_count"] == 2
            assert list(third_graph.get_state(config).next) == []

        assert calls == {"planning_guard": 1, "planning": 3, "tool_execution": 1}
        assert planner_llm.calls == 3
        assert runtime.get_failure(thread_id) is None
    finally:
        with open_postgres_checkpointer() as checkpointer:
            checkpointer.delete_thread(thread_id)


def test_workflow_routes_and_guard_accept_restored_top_level_enum_values() -> None:
    """Top-level checkpoint values may be StrEnum-compatible strings after restore."""

    state = create_initial_agent_state(_RecoveryIncident(id=uuid4()))
    state["current_stage"] = AgentStage.TOOL_EXECUTION.value

    assert workflow_module._route_after_planning(state) == "tool_execution"

    state["current_stage"] = AgentStage.EVIDENCE_EVALUATION.value
    state["evaluation_decision"] = EvaluationDecision.CONTINUE.value

    assert workflow_module._route_after_evidence_evaluation(state) == "planning_guard"

    state["current_stage"] = AgentStage.INVESTIGATION_PLANNING.value
    state["investigation_round"] = InvestigationLoopLimits().max_rounds

    guarded = workflow_module._planning_guard_node(state, InvestigationLoopLimits())

    assert guarded["evaluation_decision"] == EvaluationDecision.NEEDS_MANUAL_ACTION
