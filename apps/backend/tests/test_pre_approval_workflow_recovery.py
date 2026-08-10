"""Generic persisted failed-task inspection and same-thread continuation tests."""

from __future__ import annotations

from typing import TypedDict
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from devsupport_backend.agent.runtime import WorkflowService


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
