"""LangGraph wiring tests for the bounded Task 3.8 investigation loop."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from sqlalchemy.orm import Session

import devsupport_backend.agent.nodes.tool_execution as execution_module
import devsupport_backend.agent.workflow as workflow_module
from devsupport_backend.agent.budget import InvestigationBudget
from devsupport_backend.agent.evidence_evaluator import LLMEvidenceEvaluator
from devsupport_backend.agent.nodes.tool_execution import ToolExecutionDependencies
from devsupport_backend.agent.runtime import WorkflowService
from devsupport_backend.agent.state import (
    AgentStage,
    AgentState,
    EvaluationDecision,
    EvidenceContext,
    PolicyDecision,
    PolicyOutcome,
    PolicyReasonCode,
    ReportOutcome,
    ToolHistoryEntry,
    create_initial_agent_state,
)
from devsupport_backend.agent.workflow import (
    InvestigationLoopLimits,
    InvestigationWorkflowDependencies,
    build_investigation_graph,
    build_production_investigation_graph,
)
from devsupport_backend.models import Incident, Report
from devsupport_backend.tools.registry import ToolName
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
        self.evaluation_calls = 0
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
        if system_prompt.startswith("Evaluate whether"):
            self.evaluation_calls += 1
            return json.dumps(
                {
                    "decision": "NEEDS_MANUAL_ACTION",
                    "reason": "The controlled test evaluator requests manual action.",
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
                    "action_type": "manual_action",
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


class FakePolicyGate:
    """Keep graph wiring tests independent from database-backed Task 4.1 policy tests."""

    def __init__(self, outcome: PolicyOutcome | None = None) -> None:
        self.calls = 0
        self._outcome = outcome

    def evaluate(self, state: AgentState) -> PolicyOutcome:
        self.calls += 1
        assert state["current_stage"] is AgentStage.CONCLUSION
        assert state["final_conclusion"] is not None
        assert state["proposed_action"] is not None
        return self._outcome or PolicyOutcome(
            decision=PolicyDecision.DENIED,
            reason_code=PolicyReasonCode.MANUAL_ACTION,
            reason="The graph wiring test uses a non-executable manual action.",
        )


class FakeApprovalWait:
    """Keep Day 3 graph tests on the DENIED path without database writes or interrupts."""

    def enter_waiting_approval(self, state: AgentState) -> None:
        raise AssertionError(f"approval wait must not run for DENIED policy: {state}")

    def interrupt_payload(self, state: AgentState) -> dict[str, str]:
        raise AssertionError(f"approval interrupt must not run for DENIED policy: {state}")


class FakeApprovalDecision:
    """The decision node only runs after a later same-thread resume."""

    def resolve(self, state: AgentState) -> object:
        raise AssertionError(f"approval decision must not run before resume: {state}")


class RecordingManualTerminalizer:
    def __init__(self) -> None:
        self.calls = 0

    def mark_needs_manual_action(self, state: AgentState) -> AgentState:
        self.calls += 1
        return {**state, "current_stage": AgentStage.NEEDS_MANUAL_ACTION}


class RecordingFinalReport:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, state: AgentState) -> ReportOutcome:
        self.calls += 1
        return ReportOutcome(
            report_id=uuid4(),
            incident_id=state["incident"].id,
            final_status="NEEDS_MANUAL_ACTION",
        )


class RecordingApprovalWait:
    """Controlled interrupt payload for formal graph approval-wait wiring coverage."""

    def __init__(self, action_id: UUID) -> None:
        self._action_id = action_id
        self.entered = 0
        self.payloads = 0

    def enter_waiting_approval(self, state: AgentState) -> None:
        self.entered += 1
        assert state["policy_outcome"] is not None
        assert state["policy_outcome"].action_id == self._action_id

    def interrupt_payload(self, state: AgentState) -> dict[str, str]:
        self.payloads += 1
        assert state["current_stage"] is AgentStage.WAITING_APPROVAL
        return {
            "incident_id": str(state["incident"].id),
            "action_id": str(self._action_id),
            "action_type": "rollback_deployment",
            "service": "order-service",
            "environment": "local",
            "current_version": "v1.1.0",
            "target_version": "v1.0.0",
            "reason": "Verified Action requires human approval.",
        }


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


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
    evaluator: FakeEvaluator | LLMEvidenceEvaluator,
    *,
    policy_gate: FakePolicyGate | None = None,
    approval_wait: FakeApprovalWait | RecordingApprovalWait | None = None,
    final_report: RecordingFinalReport | None = None,
    manual_terminalizer: RecordingManualTerminalizer | None = None,
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
        policy_gate=policy_gate or FakePolicyGate(),
        approval_wait=approval_wait or FakeApprovalWait(),
        approval_decision=FakeApprovalDecision(),
        final_report=final_report,
        manual_terminalizer=manual_terminalizer,
    )


def _run_workflow(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tool_outputs: list[QueryMetricsOutput],
    evaluator: FakeEvaluator | LLMEvidenceEvaluator,
    limits: InvestigationLoopLimits | None = None,
    budget: InvestigationBudget | None = None,
    policy_gate: FakePolicyGate | None = None,
    final_report: RecordingFinalReport | None = None,
    manual_terminalizer: RecordingManualTerminalizer | None = None,
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
        _workflow_dependencies(
            llm_client,
            evaluator,
            policy_gate=policy_gate,
            final_report=final_report,
            manual_terminalizer=manual_terminalizer,
        ),
        limits=limits,
        budget=budget,
    )
    return graph.invoke(_build_initial_state()), llm_client, (retrieval_calls, tool_calls)


def test_graph_compiles() -> None:
    evaluator = FakeEvaluator([EvaluationDecision.CONCLUDE])

    graph = build_investigation_graph(_workflow_dependencies(WorkflowFakeLLM(), evaluator))

    assert "evidence_evaluation" in graph.get_graph().nodes
    assert "tool_execution" in graph.get_graph().nodes
    assert "policy_gate" in graph.get_graph().nodes
    assert "approval_wait" in graph.get_graph().nodes


def test_default_limits_allow_five_investigation_rounds_with_initial_retrieval_budget() -> None:
    limits = InvestigationLoopLimits()

    assert limits.max_rounds == 5
    assert limits.max_tool_calls == 6
    assert limits.max_rounds + 1 == limits.max_tool_calls


def test_initial_evidence_batch_skips_intermediate_llm_update_after_first_runtime_probe() -> None:
    state = _build_initial_state()
    state.update(
        {
            "current_stage": AgentStage.HYPOTHESIS_UPDATE,
            "tool_history": [
                ToolHistoryEntry(
                    tool_name=ToolName.SEARCH_KNOWLEDGE,
                    tool_arguments={"query": "catalog errors"},
                    status=ToolStatus.SUCCESS,
                ),
                ToolHistoryEntry(
                    tool_name=ToolName.QUERY_LOGS,
                    tool_arguments={"service": "catalog-service", "environment": "local"},
                    status=ToolStatus.SUCCESS,
                ),
            ],
        }
    )

    assert workflow_module._should_collect_complementary_initial_probe(state) is True


def test_workflow_service_default_budget_reaches_five_round_terminalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The five-round default must not exhaust LangGraph before its guard runs."""
    tool_outputs = [_successful_metrics_output() for _ in range(5)]
    tool_calls = 0

    def fake_query_metrics(*_: object) -> QueryMetricsOutput:
        nonlocal tool_calls
        tool_calls += 1
        return tool_outputs.pop(0)

    monkeypatch.setattr(workflow_module, "retrieval_node", _fake_retrieval)
    monkeypatch.setattr(execution_module, "query_metrics", fake_query_metrics)
    terminalizer = RecordingManualTerminalizer()
    report = RecordingFinalReport()
    graph = build_investigation_graph(
        _workflow_dependencies(
            WorkflowFakeLLM(),
            FakeEvaluator([EvaluationDecision.CONTINUE] * 5),
            final_report=report,
            manual_terminalizer=terminalizer,
        ),
        budget=InvestigationBudget(max_llm_calls=20),
    )
    incident = Incident(
        id=uuid4(),
        service="catalog-service",
        environment="staging",
        description="Five successful investigation rounds must reach the safety guard.",
        time_range_start=datetime(2026, 8, 8, 10, 0, tzinfo=UTC),
        time_range_end=datetime(2026, 8, 8, 10, 5, tzinfo=UTC),
        thread_id=str(uuid4()),
    )

    result = WorkflowService(graph).start(incident)

    assert result["current_stage"] is AgentStage.NEEDS_MANUAL_ACTION
    assert result["investigation_round"] == 5
    assert result["tool_call_count"] == 5
    assert tool_calls == 5
    assert terminalizer.calls == 1
    assert report.calls == 1


def test_default_planning_guard_preserves_checkpoint_equivalent_remaining_budget() -> None:
    limits = InvestigationLoopLimits()
    state = _build_initial_state()
    state["current_stage"] = AgentStage.INVESTIGATION_PLANNING
    state["investigation_round"] = 3
    state["tool_call_count"] = 4
    state["evaluation_decision"] = EvaluationDecision.CONTINUE

    allowed = workflow_module._planning_guard_node(state, limits)

    assert allowed is state
    assert allowed["evaluation_decision"] is EvaluationDecision.CONTINUE

    at_round_limit = {**state, "investigation_round": 5}
    round_limited = workflow_module._planning_guard_node(at_round_limit, limits)
    assert round_limited["evaluation_decision"] is EvaluationDecision.NEEDS_MANUAL_ACTION

    at_tool_limit = {**state, "tool_call_count": 6}
    tool_limited = workflow_module._planning_guard_node(at_tool_limit, limits)
    assert tool_limited["evaluation_decision"] is EvaluationDecision.NEEDS_MANUAL_ACTION


def test_graph_compiles_with_an_injected_checkpointer() -> None:
    evaluator = FakeEvaluator([EvaluationDecision.CONCLUDE])

    graph = build_investigation_graph(
        _workflow_dependencies(WorkflowFakeLLM(), evaluator),
        checkpointer=InMemorySaver(),
    )

    assert "evidence_evaluation" in graph.get_graph().nodes


def test_active_budget_after_retrieval_routes_to_manual_terminalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    terminalizer = RecordingManualTerminalizer()
    report = RecordingFinalReport()

    def delayed_retrieval(state: AgentState, rag_service: object) -> AgentState:
        clock.advance(95.0)
        return _fake_retrieval(state, rag_service)

    monkeypatch.setattr(workflow_module, "retrieval_node", delayed_retrieval)
    graph = build_investigation_graph(
        _workflow_dependencies(
            WorkflowFakeLLM(),
            FakeEvaluator([EvaluationDecision.CONCLUDE]),
            final_report=report,
            manual_terminalizer=terminalizer,
        ),
        budget=InvestigationBudget(max_active_execution_seconds=95.0),
    )
    source = _build_initial_state()["incident"]
    incident = Incident(
        id=uuid4(),
        service=source.service,
        environment=source.environment,
        description=source.description,
        time_range_start=source.time_range_start,
        time_range_end=source.time_range_end,
        thread_id=str(uuid4()),
    )

    result = WorkflowService(
        graph,
        InvestigationBudget(max_active_execution_seconds=95.0),
        monotonic_clock=clock,
    ).start(incident)

    assert result["evaluation_decision"] is EvaluationDecision.NEEDS_MANUAL_ACTION
    assert result["active_execution_seconds"] == 95.0
    assert terminalizer.calls == 1
    assert report.calls == 1


def test_approval_required_routes_to_a_real_langgraph_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action_id = uuid4()
    approval_wait = RecordingApprovalWait(action_id)
    policy_gate = FakePolicyGate(
        PolicyOutcome(
            decision=PolicyDecision.APPROVAL_REQUIRED,
            reason_code=PolicyReasonCode.APPROVAL_REQUIRED,
            reason="Verified Action requires human approval.",
            action_id=action_id,
        )
    )
    monkeypatch.setattr(workflow_module, "retrieval_node", _fake_retrieval)
    monkeypatch.setattr(execution_module, "query_metrics", lambda *_: _successful_metrics_output())
    graph = build_investigation_graph(
        _workflow_dependencies(
            WorkflowFakeLLM(),
            FakeEvaluator([EvaluationDecision.CONCLUDE]),
            policy_gate=policy_gate,
            approval_wait=approval_wait,
        ),
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": str(uuid4())}}

    interrupted = graph.invoke(_build_initial_state(), config)
    paused = graph.get_state(config).values

    assert "__interrupt__" in interrupted
    assert interrupted["__interrupt__"][0].value["action_id"] == str(action_id)
    assert paused["current_stage"] is AgentStage.WAITING_APPROVAL
    assert paused["policy_outcome"] is not None
    assert paused["policy_outcome"].action_id == action_id
    assert approval_wait.entered == 1
    assert approval_wait.payloads == 1


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
    assert result["current_stage"] is AgentStage.POLICY_GATE
    assert result["final_conclusion"] is not None
    assert result["proposed_action"] is not None
    assert result["policy_outcome"] is not None
    assert result["policy_outcome"].decision is PolicyDecision.DENIED
    assert result["investigation_round"] == 2
    assert result["tool_call_count"] == 2
    assert result["llm_call_count"] == 6
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
    assert result["llm_call_count"] == 3
    assert llm_client.planning_calls == 1
    assert llm_client.update_calls == 1
    assert llm_client.resolution_calls == 0
    assert evaluator.calls == 1
    assert calls == (1, 1)


def test_llm_budget_allows_the_last_call_then_terminalizes_before_the_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminalizer = RecordingManualTerminalizer()
    report = RecordingFinalReport()

    result, llm_client, calls = _run_workflow(
        monkeypatch,
        tool_outputs=[_successful_metrics_output()],
        evaluator=FakeEvaluator([EvaluationDecision.CONCLUDE]),
        budget=InvestigationBudget(max_llm_calls=1),
        final_report=report,
        manual_terminalizer=terminalizer,
    )

    assert result["llm_call_count"] == 1
    assert result["evaluation_decision"] is EvaluationDecision.NEEDS_MANUAL_ACTION
    assert result["current_stage"] is AgentStage.NEEDS_MANUAL_ACTION
    assert llm_client.generation_calls == 1
    assert llm_client.update_calls == 0
    assert terminalizer.calls == 1
    assert report.calls == 1
    assert calls == (1, 0)


def test_llm_budget_blocks_the_ninth_call_without_invoking_the_delegate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workflow_module, "retrieval_node", _fake_retrieval)
    monkeypatch.setattr(execution_module, "query_metrics", lambda *_: _successful_metrics_output())
    terminalizer = RecordingManualTerminalizer()
    report = RecordingFinalReport()
    llm_client = WorkflowFakeLLM()
    graph = build_investigation_graph(
        _workflow_dependencies(
            llm_client,
            FakeEvaluator([EvaluationDecision.CONCLUDE]),
            final_report=report,
            manual_terminalizer=terminalizer,
        ),
        budget=InvestigationBudget(max_llm_calls=8),
    )
    state = _build_initial_state()
    state["llm_call_count"] = 8

    result = graph.invoke(state)

    assert result["llm_call_count"] == 8
    assert result["current_stage"] is AgentStage.NEEDS_MANUAL_ACTION
    assert llm_client.generation_calls == 0
    assert terminalizer.calls == 1
    assert report.calls == 1


def test_llm_budget_allows_the_eighth_call_before_blocking_the_ninth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminalizer = RecordingManualTerminalizer()
    report = RecordingFinalReport()
    monkeypatch.setattr(workflow_module, "retrieval_node", _fake_retrieval)
    llm_client = WorkflowFakeLLM()
    graph = build_investigation_graph(
        _workflow_dependencies(
            llm_client,
            FakeEvaluator([EvaluationDecision.CONCLUDE]),
            final_report=report,
            manual_terminalizer=terminalizer,
        ),
        budget=InvestigationBudget(max_llm_calls=8),
    )
    state = _build_initial_state()
    state["llm_call_count"] = 7

    result = graph.invoke(state)

    assert result["llm_call_count"] == 8
    assert result["current_stage"] is AgentStage.NEEDS_MANUAL_ACTION
    assert llm_client.generation_calls == 1
    assert llm_client.planning_calls == 0
    assert terminalizer.calls == 1
    assert report.calls == 1


def test_llm_budget_blocks_the_production_evidence_evaluator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminalizer = RecordingManualTerminalizer()
    report = RecordingFinalReport()

    result, llm_client, _ = _run_workflow(
        monkeypatch,
        tool_outputs=[_successful_metrics_output()],
        evaluator=LLMEvidenceEvaluator(WorkflowFakeLLM()),
        budget=InvestigationBudget(max_llm_calls=3),
        final_report=report,
        manual_terminalizer=terminalizer,
    )

    assert result["llm_call_count"] == 3
    assert result["current_stage"] is AgentStage.NEEDS_MANUAL_ACTION
    assert llm_client.generation_calls == 1
    assert llm_client.update_calls == 1
    assert llm_client.evaluation_calls == 0
    assert terminalizer.calls == 1
    assert report.calls == 1


def test_llm_budget_blocks_resolution_proposal_after_conclusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminalizer = RecordingManualTerminalizer()
    report = RecordingFinalReport()

    result, llm_client, _ = _run_workflow(
        monkeypatch,
        tool_outputs=[_successful_metrics_output()],
        evaluator=FakeEvaluator([EvaluationDecision.CONCLUDE]),
        budget=InvestigationBudget(max_llm_calls=2),
        final_report=report,
        manual_terminalizer=terminalizer,
    )

    assert result["llm_call_count"] == 2
    assert result["current_stage"] is AgentStage.NEEDS_MANUAL_ACTION
    assert llm_client.resolution_calls == 0
    assert terminalizer.calls == 1
    assert report.calls == 1


def test_deterministic_initial_plan_does_not_consume_or_trigger_llm_budget() -> None:
    state = _build_initial_state()
    state["incident"] = state["incident"].model_copy(update={"environment": "local"})
    state["current_stage"] = AgentStage.INVESTIGATION_PLANNING
    state["llm_call_count"] = 8
    state["tool_history"] = [
        ToolHistoryEntry(
            tool_name=ToolName.SEARCH_KNOWLEDGE,
            tool_arguments={"query": "catalog errors"},
            status=ToolStatus.SUCCESS,
        )
    ]
    llm_client = WorkflowFakeLLM()

    result = workflow_module._investigation_planning_node(
        state,
        llm_client,
        InvestigationBudget(max_llm_calls=8),
    )

    assert result["current_stage"] is AgentStage.TOOL_EXECUTION
    assert result["llm_call_count"] == 8
    assert llm_client.planning_calls == 0


@pytest.mark.parametrize(
    ("evaluator", "limits", "policy_gate"),
    [
        (FakeEvaluator([EvaluationDecision.NEEDS_MANUAL_ACTION]), None, None),
        (
            FakeEvaluator([EvaluationDecision.CONTINUE]),
            InvestigationLoopLimits(max_rounds=1, max_tool_calls=5),
            None,
        ),
        (
            FakeEvaluator([EvaluationDecision.CONCLUDE]),
            None,
            FakePolicyGate(
                PolicyOutcome(
                    decision=PolicyDecision.DENIED,
                    reason_code=PolicyReasonCode.MANUAL_ACTION,
                    reason="No executable action is allowed.",
                )
            ),
        ),
    ],
    ids=["evidence_manual", "planning_limit", "policy_denied"],
)
def test_formal_terminal_paths_persist_manual_stage_then_generate_report(
    monkeypatch: pytest.MonkeyPatch,
    evaluator: FakeEvaluator,
    limits: InvestigationLoopLimits | None,
    policy_gate: FakePolicyGate | None,
) -> None:
    terminalizer = RecordingManualTerminalizer()
    report = RecordingFinalReport()
    result, _, _ = _run_workflow(
        monkeypatch,
        tool_outputs=[_successful_metrics_output()],
        evaluator=evaluator,
        limits=limits,
        policy_gate=policy_gate,
        final_report=report,
        manual_terminalizer=terminalizer,
    )

    assert result["current_stage"] is AgentStage.NEEDS_MANUAL_ACTION
    assert result["report_outcome"] is not None
    assert terminalizer.calls == 1
    assert report.calls == 1


def test_production_formal_factory_uses_terminalization_and_report_services(
    monkeypatch: pytest.MonkeyPatch, database_session: Session
) -> None:
    now = datetime.now(UTC)
    incident = Incident(
        service="catalog-service",
        environment="staging",
        description="Production factory terminal route test.",
        time_range_start=now,
        time_range_end=now + timedelta(minutes=5),
        thread_id=str(uuid4()),
    )
    database_session.add(incident)
    database_session.commit()
    monkeypatch.setattr(workflow_module, "retrieval_node", _fake_retrieval)
    monkeypatch.setattr(
        execution_module, "query_metrics", lambda *_: _successful_metrics_output()
    )
    graph = build_production_investigation_graph(
        _workflow_dependencies(
            WorkflowFakeLLM(),
            FakeEvaluator([EvaluationDecision.NEEDS_MANUAL_ACTION]),
        ),
        session=database_session,
    )

    result = WorkflowService(graph).start(incident)

    database_session.refresh(incident)
    assert incident.status == "NEEDS_MANUAL_ACTION"
    assert result["current_stage"] is AgentStage.NEEDS_MANUAL_ACTION
    assert result["report_outcome"] is not None
    report = database_session.scalar(select(Report).where(Report.incident_id == incident.id))
    assert report is not None


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
    assert result["current_stage"] is AgentStage.POLICY_GATE
    assert result["final_conclusion"] is not None
    assert result["proposed_action"] is not None
    assert result["policy_outcome"] is not None
    assert result["investigation_round"] == 1
    assert result["tool_call_count"] == 1
    assert result["llm_call_count"] == 4
    assert llm_client.planning_calls == 1
    assert llm_client.update_calls == 1
    assert llm_client.resolution_calls == 1
    assert evaluator.calls == 1
    assert calls == (1, 1)
