"""Budget contract and checkpoint-safe usage accounting tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from devsupport_backend.agent.budget import (
    ACTIVE_EXECUTION_SAFETY_MARGIN_SECONDS,
    DEFAULT_INVESTIGATION_BUDGET,
    ActiveExecutionBudgetExceeded,
    InvestigationBudget,
    UsageAccountingLLMClient,
    active_execution_scope,
    effective_llm_timeout_seconds,
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
    _enforce_active_execution_budget_node,
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


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


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


def test_budget_freezes_initial_v1_limits_with_calibrated_active_time() -> None:
    budget = DEFAULT_INVESTIGATION_BUDGET
    limits = InvestigationLoopLimits()

    assert budget.max_rounds == DEFAULT_MAX_INVESTIGATION_ROUNDS == 5
    assert budget.max_tool_calls == DEFAULT_MAX_TOOL_CALLS == 6
    assert budget.max_llm_calls == 8
    assert budget.max_workflow_retries == 1
    assert budget.max_active_execution_seconds == 95.0
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


def test_active_execution_usage_accumulates_across_resume_but_excludes_approval_wait() -> None:
    incident = _incident()
    clock = FakeClock()
    budget = InvestigationBudget(max_active_execution_seconds=95.0)
    graph = StateGraph(AgentState)

    def first_work(state: AgentState) -> AgentState:
        clock.advance(40.0)
        return state

    def waiting_for_approval(state: AgentState) -> AgentState:
        interrupt("approval required")
        return state

    def resumed_work(state: AgentState) -> AgentState:
        clock.advance(5.0)
        return state

    graph.add_node("first_work", _enforce_active_execution_budget_node(first_work, budget))
    graph.add_node("approval_wait", waiting_for_approval)
    graph.add_node("resumed_work", _enforce_active_execution_budget_node(resumed_work, budget))
    graph.add_edge(START, "first_work")
    graph.add_edge("first_work", "approval_wait")
    graph.add_edge("approval_wait", "resumed_work")
    graph.add_edge("resumed_work", END)
    service = WorkflowService(
        graph.compile(checkpointer=InMemorySaver()), budget, monotonic_clock=clock
    )

    service.start(incident)
    assert service.get_state(incident.thread_id)["active_execution_seconds"] == 40.0

    clock.advance(1_000.0)
    result = service.resume(incident.thread_id, {"approved": True})

    assert result["active_execution_seconds"] == 45.0
    assert service.get_state(incident.thread_id)["active_execution_seconds"] == 45.0


def test_active_execution_budget_allows_work_below_limit_and_stops_at_exact_boundary() -> None:
    clock = FakeClock()
    budget = InvestigationBudget(max_active_execution_seconds=95.0)
    calls = 0

    def work(state: AgentState) -> AgentState:
        nonlocal calls
        calls += 1
        return state

    guarded_work = _enforce_active_execution_budget_node(work, budget)
    below_limit = create_initial_agent_state(_incident())
    below_limit["active_execution_seconds"] = 94.0
    with active_execution_scope(94.0, budget, clock=clock):
        assert guarded_work(below_limit)["evaluation_decision"] is None

    at_limit = create_initial_agent_state(_incident())
    at_limit["active_execution_seconds"] = 95.0
    with active_execution_scope(95.0, budget, clock=clock):
        result = guarded_work(at_limit)

    assert calls == 1
    assert result["evaluation_decision"] is EvaluationDecision.NEEDS_MANUAL_ACTION
    assert result["active_execution_seconds"] == 95.0


def test_active_execution_budget_stops_after_a_node_reaches_the_exact_boundary() -> None:
    clock = FakeClock()
    budget = InvestigationBudget(max_active_execution_seconds=95.0)
    state = create_initial_agent_state(_incident())
    state["active_execution_seconds"] = 94.0

    def work(current: AgentState) -> AgentState:
        clock.advance(1.0)
        return current

    with active_execution_scope(94.0, budget, clock=clock):
        result = _enforce_active_execution_budget_node(work, budget)(state)

    assert result["evaluation_decision"] is EvaluationDecision.NEEDS_MANUAL_ACTION
    assert result["active_execution_seconds"] == 95.0


def test_pre_active_budget_checkpoint_defaults_to_zero_usage() -> None:
    clock = FakeClock()
    budget = InvestigationBudget(max_active_execution_seconds=95.0)
    state = create_initial_agent_state(_incident())
    del state["active_execution_seconds"]

    def work(current: AgentState) -> AgentState:
        clock.advance(3.0)
        return current

    with active_execution_scope(0.0, budget, clock=clock):
        result = _enforce_active_execution_budget_node(work, budget)(state)

    assert result["active_execution_seconds"] == 3.0


def test_effective_llm_timeout_respects_remaining_active_execution_budget() -> None:
    budget = InvestigationBudget(max_active_execution_seconds=95.0)
    clock = FakeClock()

    with active_execution_scope(25.0, budget, clock=clock):
        assert effective_llm_timeout_seconds(50.0) == 50.0
    with active_execution_scope(75.0, budget, clock=clock):
        assert effective_llm_timeout_seconds(50.0) == 15.0
    with active_execution_scope(90.0, budget, clock=clock):
        with pytest.raises(ActiveExecutionBudgetExceeded):
            effective_llm_timeout_seconds(50.0)

    assert ACTIVE_EXECUTION_SAFETY_MARGIN_SECONDS == 5.0
    assert effective_llm_timeout_seconds(50.0) == 50.0


def test_budget_limited_llm_timeout_routes_to_manual_without_a_failed_task() -> None:
    incident = _incident()
    budget = InvestigationBudget(max_active_execution_seconds=95.0)
    clock = FakeClock()
    llm_client = UsageAccountingLLMClient(_BudgetLimitedTimeoutLLM())
    graph = StateGraph(AgentState)

    def invoke_llm(state: AgentState) -> AgentState:
        llm_client.complete(system_prompt="system", user_prompt="user")
        return state

    graph.add_node(
        "invoke_llm",
        _enforce_active_execution_budget_node(_account_llm_usage_node(invoke_llm), budget),
    )
    graph.add_edge(START, "invoke_llm")
    graph.add_edge("invoke_llm", END)
    compiled = graph.compile(checkpointer=InMemorySaver())
    state = create_initial_agent_state(incident)
    state["active_execution_seconds"] = 75.0
    config = WorkflowService.config_for(incident.thread_id)

    with active_execution_scope(75.0, budget, clock=clock):
        result = compiled.invoke(state, config)

    assert result["evaluation_decision"] is EvaluationDecision.NEEDS_MANUAL_ACTION
    assert list(compiled.get_state(config).tasks) == []


def test_active_execution_usage_carries_into_retry_without_a_new_full_budget() -> None:
    incident = _incident()
    clock = FakeClock()
    budget = InvestigationBudget(max_active_execution_seconds=95.0)
    should_fail = [True]
    graph = StateGraph(AgentState)

    def investigation_work(state: AgentState) -> AgentState:
        if should_fail[0]:
            raise RuntimeError("controlled retryable failure")
        clock.advance(10.0)
        return state

    graph.add_node(
        "investigation_work",
        _enforce_active_execution_budget_node(investigation_work, budget),
    )
    graph.add_edge(START, "investigation_work")
    graph.add_edge("investigation_work", END)
    compiled = graph.compile(checkpointer=InMemorySaver())
    service = WorkflowService(compiled, budget, monotonic_clock=clock)
    state = create_initial_agent_state(incident)
    state["active_execution_seconds"] = 70.0

    with active_execution_scope(70.0, budget, clock=clock):
        with pytest.raises(RuntimeError, match="controlled retryable failure"):
            compiled.invoke(state, WorkflowService.config_for(incident.thread_id))

    service.record_retry_attempt(incident.thread_id)
    should_fail[0] = False
    result = service.retry_failed_task(incident.thread_id)

    assert result["active_execution_seconds"] == 80.0
    assert result["workflow_retry_count"] == 1


def test_failed_invocation_persists_active_usage_and_retries_from_the_remaining_budget() -> None:
    incident = _incident()
    clock = FakeClock()
    budget = InvestigationBudget(max_active_execution_seconds=95.0)
    calls = {"predecessor": 0, "planning": 0, "terminal": 0}
    retry_timeouts: list[float] = []
    graph = StateGraph(AgentState)

    def predecessor(state: AgentState) -> AgentState:
        calls["predecessor"] += 1
        clock.advance(20.0)
        return state

    def investigation_planning(state: AgentState) -> AgentState:
        calls["planning"] += 1
        if calls["planning"] == 1:
            clock.advance(50.0)
            raise RuntimeError("controlled failed LLM invocation")
        retry_timeouts.append(effective_llm_timeout_seconds(50.0))
        return state

    def terminal(state: AgentState) -> AgentState:
        calls["terminal"] += 1
        return state

    graph.add_node("predecessor", _enforce_active_execution_budget_node(predecessor, budget))
    graph.add_node(
        "investigation_planning",
        _enforce_active_execution_budget_node(investigation_planning, budget),
    )
    graph.add_node("terminal", _enforce_active_execution_budget_node(terminal, budget))
    graph.add_edge(START, "predecessor")
    graph.add_edge("predecessor", "investigation_planning")
    graph.add_edge("investigation_planning", "terminal")
    graph.add_edge("terminal", END)
    service = WorkflowService(
        graph.compile(checkpointer=InMemorySaver()), budget, monotonic_clock=clock
    )

    with pytest.raises(RuntimeError, match="controlled failed LLM invocation"):
        service.start(incident)

    failed_state = service.get_state(incident.thread_id)
    failure = service.get_failure(incident.thread_id)
    assert failed_state["active_execution_seconds"] == 70.0
    assert failure is not None
    assert failure.failed_node == "investigation_planning"
    assert calls == {"predecessor": 1, "planning": 1, "terminal": 0}

    service.record_retry_attempt(incident.thread_id)
    result = service.retry_failed_task(incident.thread_id)

    assert retry_timeouts == [20.0]
    assert result["active_execution_seconds"] == 70.0
    assert result["workflow_retry_count"] == 1
    assert calls == {"predecessor": 1, "planning": 2, "terminal": 1}
    assert service.get_failure(incident.thread_id) is None


def test_second_failed_retry_accumulates_active_usage_and_remains_continuable() -> None:
    incident = _incident()
    clock = FakeClock()
    budget = InvestigationBudget(max_active_execution_seconds=95.0)
    calls = {"predecessor": 0, "planning": 0}
    graph = StateGraph(AgentState)

    def predecessor(state: AgentState) -> AgentState:
        calls["predecessor"] += 1
        clock.advance(20.0)
        return state

    def investigation_planning(state: AgentState) -> AgentState:
        del state
        calls["planning"] += 1
        clock.advance(50.0 if calls["planning"] == 1 else 20.0)
        raise RuntimeError("controlled retryable failure")

    graph.add_node("predecessor", _enforce_active_execution_budget_node(predecessor, budget))
    graph.add_node(
        "investigation_planning",
        _enforce_active_execution_budget_node(investigation_planning, budget),
    )
    graph.add_edge(START, "predecessor")
    graph.add_edge("predecessor", "investigation_planning")
    graph.add_edge("investigation_planning", END)
    service = WorkflowService(
        graph.compile(checkpointer=InMemorySaver()), budget, monotonic_clock=clock
    )

    with pytest.raises(RuntimeError, match="controlled retryable failure"):
        service.start(incident)
    service.record_retry_attempt(incident.thread_id)
    with pytest.raises(RuntimeError, match="controlled retryable failure"):
        service.retry_failed_task(incident.thread_id)

    failed_state = service.get_state(incident.thread_id)
    failure = service.get_failure(incident.thread_id)
    assert failed_state["active_execution_seconds"] == 90.0
    assert failed_state["workflow_retry_count"] == 1
    assert failure is not None
    assert failure.failed_node == "investigation_planning"
    assert calls == {"predecessor": 1, "planning": 2}


def test_failed_retry_from_pre_active_budget_checkpoint_starts_at_zero() -> None:
    incident = _incident()
    clock = FakeClock()
    graph = StateGraph(AgentState)
    attempts = [0]

    def investigation_planning(state: AgentState) -> AgentState:
        del state
        attempts[0] += 1
        if attempts[0] == 2:
            clock.advance(3.0)
        raise RuntimeError("controlled legacy checkpoint failure")

    graph.add_node("investigation_planning", investigation_planning)
    graph.add_edge(START, "investigation_planning")
    graph.add_edge("investigation_planning", END)
    compiled = graph.compile(checkpointer=InMemorySaver())
    state = create_initial_agent_state(incident)
    del state["active_execution_seconds"]
    config = WorkflowService.config_for(incident.thread_id)

    with pytest.raises(RuntimeError, match="controlled legacy checkpoint failure"):
        compiled.invoke(state, config)
    service = WorkflowService(compiled, monotonic_clock=clock)
    with pytest.raises(RuntimeError, match="controlled legacy checkpoint failure"):
        service.retry_failed_task(incident.thread_id)

    assert service.get_state(incident.thread_id)["active_execution_seconds"] == 3.0
    assert service.get_failure(incident.thread_id) is not None


def test_failed_resume_persists_active_usage() -> None:
    incident = _incident()
    clock = FakeClock()
    graph = StateGraph(AgentState)

    def wait_then_fail(state: AgentState) -> AgentState:
        interrupt("resume controlled failure")
        clock.advance(4.0)
        raise RuntimeError("controlled resumed workflow failure")

    graph.add_node("wait_then_fail", wait_then_fail)
    graph.add_edge(START, "wait_then_fail")
    graph.add_edge("wait_then_fail", END)
    service = WorkflowService(
        graph.compile(checkpointer=InMemorySaver()), monotonic_clock=clock
    )

    service.start(incident)
    with pytest.raises(RuntimeError, match="controlled resumed workflow failure"):
        service.resume(incident.thread_id, {"continue": True})

    assert service.get_state(incident.thread_id)["active_execution_seconds"] == 4.0
    failure = service.get_failure(incident.thread_id)
    assert failure is not None
    assert failure.failed_node == "wait_then_fail"


def test_active_usage_persistence_failure_does_not_replace_workflow_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incident = _incident()
    clock = FakeClock()
    graph = StateGraph(AgentState)

    def fails(state: AgentState) -> AgentState:
        del state
        clock.advance(7.0)
        raise RuntimeError("original workflow failure")

    graph.add_node("fails", fails)
    graph.add_edge(START, "fails")
    graph.add_edge("fails", END)
    service = WorkflowService(
        graph.compile(checkpointer=InMemorySaver()), monotonic_clock=clock
    )

    def accounting_failure(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("usage persistence failed")

    monkeypatch.setattr(service, "_update_failed_checkpoint_fields", accounting_failure)

    with pytest.raises(RuntimeError, match="original workflow failure"):
        service.start(incident)

    failure = service.get_failure(incident.thread_id)
    assert failure is not None
    assert failure.failed_node == "fails"


class _BudgetLimitedTimeoutLLM:
    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        del system_prompt, user_prompt
        raise ActiveExecutionBudgetExceeded("controlled active budget timeout")
