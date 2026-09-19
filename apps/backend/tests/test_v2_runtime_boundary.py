"""Regression coverage for the formal V2 read-only runtime boundary."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import devsupport_backend.agent.nodes.tool_execution as execution_module
import devsupport_backend.agent.workflow as workflow_module
import devsupport_backend.workflow_console as workflow_console_module
from devsupport_backend.agent.budget import InvestigationBudget
from devsupport_backend.agent.nodes.tool_execution import ToolExecutionDependencies
from devsupport_backend.agent.state import (
    AgentStage,
    EvidenceContext,
    FinalConclusion,
    HypothesisContext,
    HypothesisStatus,
    PendingToolCall,
    TerminalReason,
    ToolHistoryEntry,
    create_initial_agent_state,
)
from devsupport_backend.agent.structured_output import StructuredOutputParseError
from devsupport_backend.agent.v2_terminalization import V2Terminalizer
from devsupport_backend.agent.workflow import (
    InvestigationLoopLimits,
    V2InvestigationWorkflowDependencies,
    build_v2_production_investigation_graph,
)
from devsupport_backend.investigation_lifecycle import InvestigationLifecycleService
from devsupport_backend.investigation_status import InvestigationStatus
from devsupport_backend.main import app
from devsupport_backend.models import (
    Action,
    Approval,
    Evidence,
    Incident,
    InvestigationRound,
    Report,
    Verification,
)
from devsupport_backend.tools.registry import (
    V2_READ_ONLY_TOOL_NAMES,
    ToolName,
    UnknownToolError,
    v2_tool_registry,
)
from devsupport_backend.tools.schemas import (
    CitationOutput,
    MetricSnapshot,
    QueryMetricsOutput,
    RuntimeEvidenceProvenance,
    ToolError,
    ToolStatus,
)
from devsupport_backend.workflow_console import PostgresWorkflowRuntime


class _UnusedLLM:
    def complete(self, **_: object) -> str:
        raise AssertionError("this graph-shape test must not call the LLM")


class _UnusedEvaluator:
    def evaluate(self, _: object) -> object:
        raise AssertionError("this graph-shape test must not evaluate evidence")


def _incident(session: Session) -> tuple[Incident, InvestigationRound]:
    now = datetime.now(UTC)
    incident = Incident(
        service="order-service",
        environment="local",
        description="订单接口持续返回 500。",
        time_range_start=now,
        time_range_end=now + timedelta(minutes=5),
        thread_id=str(uuid4()),
    )
    session.add(incident)
    session.commit()
    round_record = session.scalar(
        select(InvestigationRound).where(
            InvestigationRound.incident_id == incident.id,
            InvestigationRound.thread_id == incident.thread_id,
        )
    )
    assert round_record is not None
    InvestigationLifecycleService(session).start(round_record.id)
    return incident, round_record


def _state(incident: Incident, round_record: InvestigationRound) -> dict[str, object]:
    state = create_initial_agent_state(incident)
    state["round_id"] = round_record.id
    return state


def _v2_dependencies() -> V2InvestigationWorkflowDependencies:
    return V2InvestigationWorkflowDependencies(
        rag_service=object(),  # type: ignore[arg-type]
        llm_client=_UnusedLLM(),  # type: ignore[arg-type]
        tool_execution=SimpleNamespace(available_tools=V2_READ_ONLY_TOOL_NAMES),  # type: ignore[arg-type]
        evaluator=_UnusedEvaluator(),  # type: ignore[arg-type]
    )


def _route_v2_graph_to_planning_guard(
    monkeypatch: pytest.MonkeyPatch, **state_updates: object
) -> None:
    monkeypatch.setattr(
        workflow_module,
        "intake_node",
        lambda state: {**state, "current_stage": AgentStage.RETRIEVAL},
    )
    monkeypatch.setattr(
        workflow_module,
        "retrieval_node",
        lambda state, _: {**state, "current_stage": AgentStage.HYPOTHESIS_GENERATION},
    )
    monkeypatch.setattr(
        workflow_module,
        "hypothesis_generation_node",
        lambda state, _: {
            **state,
            **state_updates,
            "current_stage": AgentStage.INVESTIGATION_PLANNING,
        },
    )


def test_v2_graph_has_only_investigation_and_terminal_report_nodes(
    database_session: Session,
) -> None:
    dependencies = V2InvestigationWorkflowDependencies(
        rag_service=object(),  # type: ignore[arg-type]
        llm_client=_UnusedLLM(),  # type: ignore[arg-type]
        tool_execution=SimpleNamespace(available_tools=V2_READ_ONLY_TOOL_NAMES),  # type: ignore[arg-type]
        evaluator=_UnusedEvaluator(),  # type: ignore[arg-type]
    )

    graph = build_v2_production_investigation_graph(
        dependencies,
        session=database_session,
        limits=InvestigationLoopLimits(max_rounds=1, max_tool_calls=1),
    )

    nodes = set(graph.get_graph().nodes)
    assert {
        "intake",
        "retrieval",
        "hypothesis_generation",
        "planning_guard",
        "investigation_planning",
        "tool_execution",
        "hypothesis_update",
        "evidence_evaluation",
        "conclusion",
        "conclusion_terminalization",
        "inconclusive_terminalization",
        "failure_terminalization",
    } <= nodes
    assert not nodes.intersection(
        {
            "resolution_proposal",
            "policy_gate",
            "approval_wait",
            "approval_interrupt",
            "approval_decision",
            "controlled_action_execution",
            "recovery_verification",
        }
    )
    assert set(V2InvestigationWorkflowDependencies.__dataclass_fields__) == {
        "rag_service",
        "llm_client",
        "tool_execution",
        "evaluator",
    }


def test_postgres_runtime_composes_only_v2_investigation_dependencies(
    database_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Factory:
        @classmethod
        def from_settings(cls, *_: object) -> object:
            return object()

    captured: dict[str, object] = {}
    sentinel = object()
    monkeypatch.setattr(workflow_console_module, "OpenAICompatibleLLMClient", _Factory)
    monkeypatch.setattr(workflow_console_module, "OpenAICompatibleEmbeddingClient", _Factory)
    monkeypatch.setattr(workflow_console_module, "RAGService", lambda *_: object())
    monkeypatch.setattr(
        PostgresWorkflowRuntime,
        "_tool_execution_dependencies",
        staticmethod(lambda *_: SimpleNamespace(available_tools=V2_READ_ONLY_TOOL_NAMES)),
    )

    def capture_dependencies(
        dependencies: V2InvestigationWorkflowDependencies, **_: object
    ) -> object:
        captured["dependencies"] = dependencies
        return sentinel

    monkeypatch.setattr(
        workflow_console_module, "build_v2_production_investigation_graph", capture_dependencies
    )

    result = PostgresWorkflowRuntime(database_session)._production_graph(object(), object())

    assert result is sentinel
    assert isinstance(captured["dependencies"], V2InvestigationWorkflowDependencies)


def test_v2_tool_registry_excludes_side_effect_tools() -> None:
    assert ToolName.ROLLBACK_DEPLOYMENT not in V2_READ_ONLY_TOOL_NAMES
    assert {definition.name for definition in v2_tool_registry.list()} == V2_READ_ONLY_TOOL_NAMES
    with pytest.raises(UnknownToolError):
        v2_tool_registry.get(ToolName.ROLLBACK_DEPLOYMENT)
    with pytest.raises(ValueError, match="read-only"):
        ToolExecutionDependencies(  # type: ignore[arg-type]
            rag_service=object(),
            logs_adapter=object(),
            metrics_adapter=object(),
            traces_adapter=None,
            deployment_adapter=None,
            available_tools=frozenset({ToolName.ROLLBACK_DEPLOYMENT}),
        )


def test_v2_graph_concludes_and_terminalizes_the_current_round(
    database_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incident, round_record = _incident(database_session)
    evidence = EvidenceContext(
        round_id=round_record.id,
        source="query_logs",
        evidence_type="log_query_result",
        summary="依赖连接被拒绝。",
        data={"match_count": 3},
    )
    hypothesis = HypothesisContext(
        summary="订单服务无法连接依赖服务。",
        status=HypothesisStatus.CONFIRMED,
        confidence=0.9,
        supporting_evidence_ids=[evidence.id],
    )

    monkeypatch.setattr(
        workflow_module,
        "intake_node",
        lambda state: {**state, "current_stage": AgentStage.RETRIEVAL},
    )
    monkeypatch.setattr(
        workflow_module,
        "retrieval_node",
        lambda state, _: {**state, "current_stage": AgentStage.HYPOTHESIS_GENERATION},
    )
    monkeypatch.setattr(
        workflow_module,
        "hypothesis_generation_node",
        lambda state, _: {**state, "current_stage": AgentStage.INVESTIGATION_PLANNING},
    )
    monkeypatch.setattr(
        workflow_module,
        "_investigation_planning_node",
        lambda state, *_: {**state, "current_stage": AgentStage.TOOL_EXECUTION},
    )
    monkeypatch.setattr(
        workflow_module,
        "_tool_execution_with_initial_evidence_batch",
        lambda state, *_: {**state, "current_stage": AgentStage.HYPOTHESIS_UPDATE},
    )
    monkeypatch.setattr(
        workflow_module,
        "_hypothesis_update_round_node",
        lambda state, _: {
            **state,
            "current_stage": AgentStage.EVIDENCE_EVALUATION,
            "evidence": [evidence],
            "hypotheses": [hypothesis],
        },
    )
    dependencies = V2InvestigationWorkflowDependencies(
        rag_service=object(),  # type: ignore[arg-type]
        llm_client=_UnusedLLM(),  # type: ignore[arg-type]
        tool_execution=SimpleNamespace(available_tools=V2_READ_ONLY_TOOL_NAMES),  # type: ignore[arg-type]
        evaluator=_UnusedEvaluator(),  # type: ignore[arg-type]
    )

    result = build_v2_production_investigation_graph(
        dependencies, session=database_session
    ).invoke(_state(incident, round_record))

    database_session.refresh(incident)
    database_session.refresh(round_record)
    assert result["report_outcome"] is not None
    assert incident.investigation_status is InvestigationStatus.CONCLUDED
    assert round_record.status is InvestigationStatus.CONCLUDED
    assert round_record.terminal_reason == TerminalReason.SUFFICIENT_EVIDENCE.value


@pytest.mark.parametrize(
    ("limits", "state_updates", "expected_status", "expected_reason"),
    [
        (
            InvestigationLoopLimits(max_iterations=1, max_tool_calls=5),
            {"investigation_round": 1},
            InvestigationStatus.INCONCLUSIVE,
            TerminalReason.ITERATION_BUDGET_EXHAUSTED,
        ),
        (
            InvestigationLoopLimits(max_iterations=5, max_tool_calls=1),
            {"tool_call_count": 1},
            InvestigationStatus.INCONCLUSIVE,
            TerminalReason.TOOL_BUDGET_EXHAUSTED,
        ),
        (
            InvestigationLoopLimits(max_consecutive_failures=2),
            {"consecutive_failures": 2},
            InvestigationStatus.FAILED,
            TerminalReason.REPEATED_FAILURES,
        ),
    ],
)
def test_v2_runtime_terminalizes_deterministic_budget_stop_conditions(
    database_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    limits: InvestigationLoopLimits,
    state_updates: dict[str, object],
    expected_status: InvestigationStatus,
    expected_reason: TerminalReason,
) -> None:
    incident, round_record = _incident(database_session)
    _route_v2_graph_to_planning_guard(monkeypatch, **state_updates)

    result = build_v2_production_investigation_graph(
        _v2_dependencies(), session=database_session, limits=limits
    ).invoke(_state(incident, round_record))

    database_session.refresh(incident)
    database_session.refresh(round_record)
    assert result["report_outcome"] is not None
    assert result["terminal_reason"] is expected_reason
    assert incident.investigation_status is expected_status
    assert round_record.status is expected_status
    assert round_record.terminal_reason == expected_reason.value


def test_v2_planning_guard_continues_when_counters_are_within_budget() -> None:
    state = create_initial_agent_state(
        Incident(
            id=uuid4(),
            service="order-service",
            environment="local",
            description="调查仍有可执行检查。",
            time_range_start=datetime.now(UTC),
            time_range_end=datetime.now(UTC) + timedelta(minutes=5),
        )
    )
    state.update(
        {
            "current_stage": AgentStage.INVESTIGATION_PLANNING,
            "investigation_round": 1,
            "tool_call_count": 1,
            "consecutive_failures": 1,
            "evaluation_decision": workflow_module.EvaluationDecision.CONTINUE,
        }
    )

    guarded = workflow_module._v2_planning_guard_node(
        state,
        InvestigationLoopLimits(
            max_iterations=2, max_tool_calls=2, max_consecutive_failures=2
        ),
    )

    assert guarded is state
    assert guarded["terminal_reason"] is None


def test_v2_runtime_stops_inconclusively_when_no_tool_is_executable(
    database_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    incident, round_record = _incident(database_session)
    _route_v2_graph_to_planning_guard(monkeypatch)
    dependencies = V2InvestigationWorkflowDependencies(
        rag_service=object(),  # type: ignore[arg-type]
        llm_client=_UnusedLLM(),  # type: ignore[arg-type]
        tool_execution=SimpleNamespace(available_tools=frozenset()),  # type: ignore[arg-type]
        evaluator=_UnusedEvaluator(),  # type: ignore[arg-type]
    )

    result = build_v2_production_investigation_graph(
        dependencies, session=database_session
    ).invoke(_state(incident, round_record))

    database_session.refresh(incident)
    database_session.refresh(round_record)
    assert result["terminal_reason"] is TerminalReason.NO_FURTHER_INVESTIGATION
    assert incident.investigation_status is InvestigationStatus.INCONCLUSIVE
    assert round_record.terminal_reason == TerminalReason.NO_FURTHER_INVESTIGATION.value


def test_v2_runtime_does_not_execute_a_successful_equivalent_tool_call(
    database_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RepeatToolPlanner:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, *, system_prompt: str, user_prompt: str) -> str:
            del user_prompt
            assert system_prompt.startswith("Plan exactly")
            self.calls += 1
            return (
                '{"investigation_goal":"Collect one metric snapshot.",'
                '"tool_name":"query_metrics",'
                '"tool_arguments":{"service":"order-service","environment":"local"},'
                '"reason":"Metrics can distinguish the active hypothesis."}'
            )

    incident, round_record = _incident(database_session)
    previous_call = ToolHistoryEntry(
        tool_name=ToolName.QUERY_METRICS,
        tool_arguments={"service": "order-service", "environment": "local"},
        status=ToolStatus.SUCCESS,
    )
    _route_v2_graph_to_planning_guard(monkeypatch, tool_history=[previous_call])
    monkeypatch.setattr(
        workflow_module,
        "_tool_execution_with_initial_evidence_batch",
        lambda *_: (_ for _ in ()).throw(AssertionError("duplicate call must not execute")),
    )
    planner = RepeatToolPlanner()
    dependencies = V2InvestigationWorkflowDependencies(
        rag_service=object(),  # type: ignore[arg-type]
        llm_client=planner,  # type: ignore[arg-type]
        tool_execution=SimpleNamespace(available_tools=V2_READ_ONLY_TOOL_NAMES),  # type: ignore[arg-type]
        evaluator=_UnusedEvaluator(),  # type: ignore[arg-type]
    )

    result = build_v2_production_investigation_graph(
        dependencies, session=database_session
    ).invoke(_state(incident, round_record))

    database_session.refresh(incident)
    assert planner.calls == 1
    assert result["tool_call_count"] == 0
    assert len(result["tool_history"]) == 1
    assert result["terminal_reason"] is TerminalReason.NO_FURTHER_INVESTIGATION
    assert incident.investigation_status is InvestigationStatus.INCONCLUSIVE


def test_v2_runtime_retries_timeout_within_budget_without_fabricating_evidence(
    database_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    incident, round_record = _incident(database_session)
    _route_v2_graph_to_planning_guard(monkeypatch)
    pending_call = PendingToolCall(
        investigation_goal="Collect one metric snapshot.",
        tool_name=ToolName.QUERY_METRICS,
        tool_arguments={"service": "order-service", "environment": "local"},
        reason="Metrics are a legal read-only investigation path.",
    )
    monkeypatch.setattr(
        workflow_module,
        "_investigation_planning_node",
        lambda state, *_: {
            **state,
            "pending_tool_call": pending_call,
            "current_stage": AgentStage.TOOL_EXECUTION,
        },
    )
    outputs = [
        QueryMetricsOutput(
            status=ToolStatus.FAILURE,
            error=ToolError(code="timeout", message="safe timeout", retryable=True),
        ),
            QueryMetricsOutput(
                status=ToolStatus.SUCCESS,
                provenance=RuntimeEvidenceProvenance(
                    source="test_adapter",
                    service="order-service",
                    environment="local",
                    observed_at=datetime.now(UTC),
                ),
                metrics=MetricSnapshot(
                service="order-service",
                environment="local",
                health_status="degraded",
                request_count=10,
                success_count=8,
                error_count=2,
                error_rate=0.2,
                last_request_duration_ms=20.0,
                average_request_duration_ms=15.0,
            ),
        ),
    ]
    tool_calls = 0

    def query_metrics_once(*_: object) -> QueryMetricsOutput:
        nonlocal tool_calls
        tool_calls += 1
        return outputs.pop(0)

    monkeypatch.setattr(execution_module, "query_metrics", query_metrics_once)
    monkeypatch.setattr(
        workflow_module,
        "_hypothesis_update_round_node",
        lambda state, _: {**state, "current_stage": AgentStage.EVIDENCE_EVALUATION},
    )
    monkeypatch.setattr(
        workflow_module,
        "_evidence_evaluation_with_llm_budget",
        lambda state, *_: {
            **state,
            "evaluation_decision": workflow_module.EvaluationDecision.NEEDS_MANUAL_ACTION,
        },
    )
    dependencies = V2InvestigationWorkflowDependencies(
        rag_service=object(),  # type: ignore[arg-type]
        llm_client=_UnusedLLM(),  # type: ignore[arg-type]
        tool_execution=ToolExecutionDependencies(  # type: ignore[arg-type]
            rag_service=object(),
            logs_adapter=None,
            metrics_adapter=object(),
            traces_adapter=None,
            deployment_adapter=None,
            available_tools=frozenset({ToolName.QUERY_METRICS}),
        ),
        evaluator=_UnusedEvaluator(),  # type: ignore[arg-type]
    )

    result = build_v2_production_investigation_graph(
        dependencies,
        session=database_session,
        budget=InvestigationBudget(max_workflow_retries=1),
    ).invoke(_state(incident, round_record))

    database_session.refresh(incident)
    assert tool_calls == 2
    assert result["tool_call_count"] == 2
    assert result["retry_count"] == 0
    assert result["retry_pending"] is False
    assert result["consecutive_failures"] == 0
    assert result["last_failure_category"].value == "timeout"
    assert len(result["evidence"]) == 1
    assert incident.investigation_status is InvestigationStatus.INCONCLUSIVE


def test_v2_concluded_state_routes_only_to_terminalization() -> None:
    state = create_initial_agent_state(
        Incident(
            id=uuid4(),
            service="order-service",
            environment="local",
            description="结论已经形成。",
            time_range_start=datetime.now(UTC),
            time_range_end=datetime.now(UTC) + timedelta(minutes=5),
        )
    )
    state.update(
        {
            "current_stage": AgentStage.CONCLUSION,
            "final_conclusion": FinalConclusion(
                summary="已有足够证据。",
                root_cause="已确认的运行条件。",
                confidence=0.9,
                supporting_evidence_ids=[],
            ),
            "terminal_reason": TerminalReason.SUFFICIENT_EVIDENCE,
        }
    )

    assert workflow_module._v2_route_after_conclusion(state) == "conclusion_terminalization"


def test_v2_structured_output_failure_uses_the_bounded_workflow_retry_budget() -> None:
    state = create_initial_agent_state(
        Incident(
            id=uuid4(),
            service="order-service",
            environment="local",
            description="结构化输出需要重试。",
            time_range_start=datetime.now(UTC),
            time_range_end=datetime.now(UTC) + timedelta(minutes=5),
        )
    )
    state["current_stage"] = AgentStage.HYPOTHESIS_GENERATION
    attempts = 0

    def structured_node(current: dict[str, object]) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise StructuredOutputParseError("raw provider response must not persist")
        return {**current, "current_stage": AgentStage.INVESTIGATION_PLANNING}

    bounded = workflow_module._v2_investigation_node(
        "hypothesis_generation",
        structured_node,  # type: ignore[arg-type]
        InvestigationBudget(max_workflow_retries=1),
        None,
    )

    retrying = bounded(state)
    recovered = bounded(retrying)

    assert retrying["retry_count"] == 1
    assert retrying["retry_pending"] is True
    assert retrying["last_failure_category"].value == "structured_output_failure"
    assert (
        workflow_module._v2_route(
            retrying,
            AgentStage.INVESTIGATION_PLANNING,
            "planning_guard",
            retry_target="hypothesis_generation",
        )
        == "hypothesis_generation"
    )
    assert recovered["retry_pending"] is False
    assert recovered["retry_count"] == 0
    assert recovered["current_stage"] is AgentStage.INVESTIGATION_PLANNING


@pytest.mark.parametrize(
    ("mode", "status"),
    [
        ("inconclusive", InvestigationStatus.INCONCLUSIVE),
        ("failure", InvestigationStatus.FAILED),
    ],
)
def test_v2_graph_terminalizes_bounded_and_controlled_failures(
    database_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    status: InvestigationStatus,
) -> None:
    incident, round_record = _incident(database_session)
    if mode == "inconclusive":
        monkeypatch.setattr(
            workflow_module,
            "intake_node",
            lambda state: {
                **state,
                "evaluation_decision": workflow_module.EvaluationDecision.NEEDS_MANUAL_ACTION,
                "terminal_reason": TerminalReason.INVESTIGATION_INCONCLUSIVE,
            },
        )
    else:
        monkeypatch.setattr(
            workflow_module,
            "intake_node",
            lambda _: (_ for _ in ()).throw(RuntimeError("controlled failure")),
        )
    dependencies = V2InvestigationWorkflowDependencies(
        rag_service=object(),  # type: ignore[arg-type]
        llm_client=_UnusedLLM(),  # type: ignore[arg-type]
        tool_execution=object(),  # type: ignore[arg-type]
        evaluator=_UnusedEvaluator(),  # type: ignore[arg-type]
    )

    result = build_v2_production_investigation_graph(
        dependencies, session=database_session
    ).invoke(_state(incident, round_record))

    database_session.refresh(incident)
    database_session.refresh(round_record)
    assert result["report_outcome"] is not None
    assert incident.investigation_status is status
    assert round_record.status is status


def test_v2_conclusion_terminalization_creates_report_without_remediation(
    database_session: Session,
) -> None:
    incident, round_record = _incident(database_session)
    state = _state(incident, round_record)
    evidence = EvidenceContext(
        source="query_metrics",
        evidence_type="metric_snapshot",
        summary="错误率持续升高。",
        data={"error_rate": 0.5},
    )
    hypothesis = HypothesisContext(
        summary="订单服务的依赖调用失败。",
        status=HypothesisStatus.CONFIRMED,
        confidence=0.9,
        supporting_evidence_ids=[evidence.id],
    )
    state.update(
        {
            "current_stage": AgentStage.CONCLUSION,
            "evidence": [evidence],
            "hypotheses": [hypothesis],
            "final_conclusion": FinalConclusion(
                summary="已确认依赖调用失败。",
                root_cause=hypothesis.summary,
                confidence=0.9,
                supporting_evidence_ids=[evidence.id],
                recommended_next_action="请人工检查依赖服务。",
            ),
        }
    )

    result = V2Terminalizer(database_session).terminalize(
        state, InvestigationStatus.CONCLUDED
    )

    database_session.refresh(incident)
    database_session.refresh(round_record)
    report = database_session.scalar(select(Report).where(Report.round_id == round_record.id))
    assert incident.investigation_status is InvestigationStatus.CONCLUDED
    assert round_record.status is InvestigationStatus.CONCLUDED
    assert result["report_outcome"] is not None
    assert report is not None
    assert report.content["schema_version"] == "v2"
    assert not {"action", "approval", "execution", "verification"}.intersection(report.content)
    assert "恢复" not in str(report.content)
    assert database_session.scalar(select(Action).where(Action.incident_id == incident.id)) is None
    assert (
        database_session.scalar(select(Approval).where(Approval.incident_id == incident.id))
        is None
    )
    assert (
        database_session.scalar(select(Verification).where(Verification.incident_id == incident.id))
        is None
    )


def test_v2_knowledge_evidence_binds_hypothesis_conclusion_and_report_citations(
    database_session: Session,
) -> None:
    incident, round_record = _incident(database_session)
    state = _state(incident, round_record)
    citation = CitationOutput(
        id=f"knowledge:{uuid4()}:{uuid4()}",
        document_id=uuid4(),
        chunk_id=uuid4(),
        document_title="订单服务故障排查手册",
        source="knowledge/runbooks/order-errors.md",
        source_path="knowledge/runbooks/order-errors.md",
        chunk_index=3,
        section="依赖检查",
        document_version="2026.09",
        target_id=incident.target_id,
        scope="service",
        service_id=incident.service_id,
        environment="local",
        document_reference="knowledge/runbooks/order-errors.md#chunk-3",
    )
    evidence = EvidenceContext(
        source="search_knowledge",
        evidence_type="knowledge_retrieval",
        summary="手册要求先检查订单服务到依赖服务的连接失败。",
        citation=citation,
    )
    hypothesis = HypothesisContext(
        summary="订单服务的依赖调用失败。",
        status=HypothesisStatus.CONFIRMED,
        confidence=0.9,
        supporting_evidence_ids=[evidence.id],
    )
    state.update(
        {
            "evidence": [evidence],
            "hypotheses": [hypothesis],
            "final_conclusion": FinalConclusion(
                summary="知识证据支持优先检查依赖调用。",
                root_cause=hypothesis.summary,
                confidence=0.9,
                supporting_evidence_ids=[evidence.id],
            ),
        }
    )

    V2Terminalizer(database_session).terminalize(state, InvestigationStatus.CONCLUDED)

    persisted_evidence = database_session.scalar(
        select(Evidence).where(Evidence.round_id == round_record.id)
    )
    report = database_session.scalar(select(Report).where(Report.round_id == round_record.id))
    assert persisted_evidence is not None
    assert persisted_evidence.data["citation"] == citation.model_dump(mode="json")
    assert report is not None
    assert report.content["key_evidence"][0]["citation"] == citation.model_dump(mode="json")
    assert report.content["conclusion"]["citations"] == [citation.model_dump(mode="json")]


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (InvestigationStatus.INCONCLUSIVE, TerminalReason.INVESTIGATION_INCONCLUSIVE),
        (InvestigationStatus.FAILED, TerminalReason.WORKFLOW_FAILURE),
    ],
)
def test_v2_terminalization_synchronizes_nonconclusive_statuses(
    database_session: Session,
    status: InvestigationStatus,
    reason: TerminalReason,
) -> None:
    incident, round_record = _incident(database_session)
    state = _state(incident, round_record)
    state.update({"terminal_reason": reason})

    V2Terminalizer(database_session).terminalize(state, status)

    database_session.refresh(incident)
    database_session.refresh(round_record)
    report = database_session.scalar(select(Report).where(Report.round_id == round_record.id))
    assert incident.investigation_status is status
    assert round_record.status is status
    assert report is not None
    assert report.content["final_status"] == status.value
    assert report.content["terminal_reason"] == reason.value


def test_formal_v2_incident_api_has_no_remediation_routes() -> None:
    with TestClient(app) as client:
        incident_approval = client.post(
            f"/incidents/{uuid4()}/approval", json={"decision": "APPROVE"}
        )
        legacy_approval = client.post(
            f"/legacy/incidents/{uuid4()}/approval", json={"decision": "APPROVE"}
        )
        paths = client.get("/openapi.json").json()["paths"]

    assert incident_approval.status_code == 404
    assert legacy_approval.status_code == 404
    assert not any(
        term in path for path in paths for term in ("approval", "legacy", "remediation", "recovery")
    )
