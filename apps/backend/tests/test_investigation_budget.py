"""Budget contract and checkpoint-safe usage accounting tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from devsupport_backend.agent.budget import (
    DEFAULT_INVESTIGATION_BUDGET,
    InvestigationBudget,
    UsageAccountingLLMClient,
)
from devsupport_backend.agent.runtime import WorkflowService
from devsupport_backend.agent.state import (
    AgentState,
    EvaluationDecision,
    create_initial_agent_state,
)
from devsupport_backend.agent.workflow import (
    DEFAULT_MAX_INVESTIGATION_ROUNDS,
    DEFAULT_MAX_TOOL_CALLS,
    InvestigationLoopLimits,
    _account_llm_usage_node,
    _enforce_llm_budget_node,
)
from devsupport_backend.models import Incident


class RecordingLLM:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        del system_prompt, user_prompt
        self.calls += 1
        return "completion"


class FailingLLM:
    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        del system_prompt, user_prompt
        raise RuntimeError("controlled provider failure")


def _incident() -> Incident:
    now = datetime.now(UTC)
    return Incident(
        id=uuid4(),
        service="order-service",
        environment="local",
        description="Budget usage test incident.",
        time_range_start=now,
        time_range_end=now + timedelta(minutes=5),
        thread_id=str(uuid4()),
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_rounds": 0},
        {"max_tool_calls": -1},
        {"max_llm_calls": 0},
        {"max_workflow_retries": -1},
        {"max_active_execution_seconds": 0.0},
    ],
)
def test_budget_rejects_non_positive_configured_limits(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        InvestigationBudget(**kwargs)  # type: ignore[arg-type]


def test_budget_freezes_initial_v1_discrete_limits_and_leaves_active_time_unset() -> None:
    budget = DEFAULT_INVESTIGATION_BUDGET
    limits = InvestigationLoopLimits()

    assert budget.max_rounds == DEFAULT_MAX_INVESTIGATION_ROUNDS == 5
    assert budget.max_tool_calls == DEFAULT_MAX_TOOL_CALLS == 6
    assert budget.max_llm_calls == 8
    assert budget.max_workflow_retries == 1
    assert budget.max_active_execution_seconds is None
    assert limits.budget == budget


def test_llm_usage_counts_actual_calls_once_and_persists_in_checkpoint() -> None:
    incident = _incident()
    delegate = RecordingLLM()
    llm_client = UsageAccountingLLMClient(delegate)
    graph = StateGraph(AgentState)

    def invoke_twice(state: AgentState) -> AgentState:
        llm_client.complete(system_prompt="system", user_prompt="first")
        llm_client.complete(system_prompt="system", user_prompt="second")
        return state

    graph.add_node("invoke_twice", _account_llm_usage_node(invoke_twice))
    graph.add_edge(START, "invoke_twice")
    graph.add_edge("invoke_twice", END)
    service = WorkflowService(graph.compile(checkpointer=InMemorySaver()))

    result = service.start(incident)
    recovered = service.get_state(incident.thread_id)

    assert delegate.calls == 2
    assert result["llm_call_count"] == 2
    assert recovered["llm_call_count"] == 2


def test_deterministic_node_does_not_consume_llm_usage() -> None:
    incident = _incident()
    graph = StateGraph(AgentState)
    graph.add_node("deterministic", _account_llm_usage_node(lambda state: state))
    graph.add_edge(START, "deterministic")
    graph.add_edge("deterministic", END)

    result = WorkflowService(graph.compile(checkpointer=InMemorySaver())).start(incident)

    assert result["llm_call_count"] == 0


def test_retry_usage_is_persisted_before_a_later_retry_invocation() -> None:
    incident = _incident()
    graph = StateGraph(AgentState)

    def fails(state: AgentState) -> AgentState:
        del state
        raise RuntimeError("controlled")

    graph.add_node("fails", fails)
    graph.add_edge(START, "fails")
    graph.add_edge("fails", END)
    service = WorkflowService(graph.compile(checkpointer=InMemorySaver()))

    with pytest.raises(RuntimeError, match="controlled"):
        service.start(incident)
    service.record_retry_attempt(incident.thread_id)

    assert service.get_state(incident.thread_id)["workflow_retry_count"] == 1


def test_failed_llm_attempt_does_not_reset_prior_checkpointed_usage() -> None:
    incident = _incident()
    llm_client = UsageAccountingLLMClient(FailingLLM())
    graph = StateGraph(AgentState)

    def invoke_and_fail(state: AgentState) -> AgentState:
        llm_client.complete(system_prompt="system", user_prompt="failure")
        return state

    graph.add_node("invoke_and_fail", _account_llm_usage_node(invoke_and_fail))
    graph.add_edge(START, "invoke_and_fail")
    graph.add_edge("invoke_and_fail", END)
    compiled_graph = graph.compile(checkpointer=InMemorySaver())
    state = create_initial_agent_state(incident)
    state["llm_call_count"] = 2
    config = WorkflowService.config_for(incident.thread_id)

    with pytest.raises(RuntimeError, match="controlled provider failure"):
        compiled_graph.invoke(state, config)

    assert compiled_graph.get_state(config).values["llm_call_count"] == 2


def test_llm_usage_resumes_from_checkpoint_without_resetting() -> None:
    incident = _incident()
    delegate = RecordingLLM()
    llm_client = UsageAccountingLLMClient(delegate)
    graph = StateGraph(AgentState)

    def pause_then_invoke(state: AgentState) -> AgentState:
        interrupt("resume for LLM usage accounting")
        llm_client.complete(system_prompt="system", user_prompt="resume")
        return state

    graph.add_node("pause_then_invoke", _account_llm_usage_node(pause_then_invoke))
    graph.add_edge(START, "pause_then_invoke")
    graph.add_edge("pause_then_invoke", END)
    compiled_graph = graph.compile(checkpointer=InMemorySaver())
    service = WorkflowService(compiled_graph)
    state = create_initial_agent_state(incident)
    state["llm_call_count"] = 2
    config = WorkflowService.config_for(incident.thread_id)

    compiled_graph.invoke(state, config)
    result = service.resume(incident.thread_id, {"continue": True})

    assert delegate.calls == 1
    assert result["llm_call_count"] == 3
    assert service.get_state(incident.thread_id)["llm_call_count"] == 3


def test_llm_usage_initializes_a_pre_budget_checkpoint_counter() -> None:
    incident = _incident()
    delegate = RecordingLLM()
    llm_client = UsageAccountingLLMClient(delegate)
    graph = StateGraph(AgentState)

    def invoke_once(state: AgentState) -> AgentState:
        llm_client.complete(system_prompt="system", user_prompt="legacy")
        return state

    graph.add_node("invoke_once", _account_llm_usage_node(invoke_once))
    graph.add_edge(START, "invoke_once")
    graph.add_edge("invoke_once", END)
    state = create_initial_agent_state(incident)
    del state["llm_call_count"]

    result = graph.compile(checkpointer=InMemorySaver()).invoke(
        state, WorkflowService.config_for(incident.thread_id)
    )

    assert delegate.calls == 1
    assert result["llm_call_count"] == 1


def test_llm_budget_is_enforced_after_checkpoint_resume() -> None:
    incident = _incident()
    delegate = RecordingLLM()
    llm_client = UsageAccountingLLMClient(delegate)
    graph = StateGraph(AgentState)

    def pause(state: AgentState) -> AgentState:
        interrupt("pause before budgeted call")
        return state

    def invoke_llm(state: AgentState) -> AgentState:
        llm_client.complete(system_prompt="system", user_prompt="resume")
        return state

    graph.add_node("pause", pause)
    graph.add_node(
        "invoke_llm",
        _enforce_llm_budget_node(invoke_llm, InvestigationBudget(max_llm_calls=1)),
    )
    graph.add_edge(START, "pause")
    graph.add_edge("pause", "invoke_llm")
    graph.add_edge("invoke_llm", END)
    compiled_graph = graph.compile(checkpointer=InMemorySaver())
    service = WorkflowService(compiled_graph)
    state = create_initial_agent_state(incident)
    state["llm_call_count"] = 1

    compiled_graph.invoke(state, WorkflowService.config_for(incident.thread_id))
    result = service.resume(incident.thread_id, {"continue": True})

    assert delegate.calls == 0
    assert result["llm_call_count"] == 1
    assert result["evaluation_decision"] is EvaluationDecision.NEEDS_MANUAL_ACTION
