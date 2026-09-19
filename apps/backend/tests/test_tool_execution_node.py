"""Tests for safe dispatch and compact evidence from existing read-only Tools."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

import devsupport_backend.agent.nodes.tool_execution as execution_module
from devsupport_backend.agent.budget import InvestigationBudget
from devsupport_backend.agent.nodes.tool_execution import (
    ToolExecutionDependencies,
    ToolExecutionError,
    tool_execution_node,
)
from devsupport_backend.agent.state import (
    AgentStage,
    AgentState,
    HypothesisContext,
    HypothesisStatus,
    PendingToolCall,
    create_initial_agent_state,
)
from devsupport_backend.models import Incident
from devsupport_backend.tools.registry import ToolName, tool_registry
from devsupport_backend.tools.schemas import (
    CitationOutput,
    DeploymentRecord,
    GetDeploymentHistoryOutput,
    MetricSnapshot,
    QueryLogsOutput,
    QueryMetricsOutput,
    QueryTracesOutput,
    RuntimeEvidenceProvenance,
    SearchKnowledgeOutput,
    SearchKnowledgeResult,
    ToolError,
    ToolStatus,
)

TEST_TARGET_ID = uuid4()
TEST_SERVICE_ID = uuid4()


def build_execution_state(tool_name: ToolName, arguments: dict[str, object]) -> AgentState:
    """Create a planning-approved pending call without contacting external services."""
    started_at = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
    incident = Incident(
        id=uuid4(),
        target_id=TEST_TARGET_ID,
        service_id=TEST_SERVICE_ID,
        service="order-service",
        environment="local",
        description="The order endpoint has elevated errors.",
        time_range_start=started_at,
        time_range_end=started_at + timedelta(minutes=5),
    )
    state = create_initial_agent_state(incident, symptoms=["Order endpoint errors"])
    state["hypotheses"] = [
        HypothesisContext(
            summary="A runtime signal needs another check.",
            status=HypothesisStatus.ACTIVE,
            confidence=0.5,
            next_check="Collect one additional structured signal.",
        )
    ]
    state["current_goal"] = "Collect the next relevant signal."
    state["pending_tool_call"] = PendingToolCall(
        investigation_goal=state["current_goal"],
        tool_name=tool_name,
        tool_arguments=arguments,
        reason="The check can refine the current evidence.",
    )
    state["current_stage"] = AgentStage.TOOL_EXECUTION
    return state


def fake_dependencies() -> ToolExecutionDependencies:
    """Supply opaque fakes because Tool functions are monkeypatched at their boundary."""
    return ToolExecutionDependencies(  # type: ignore[arg-type]
        rag_service=object(),
        logs_adapter=object(),
        metrics_adapter=object(),
        traces_adapter=object(),
        deployment_adapter=object(),
    )


def successful_output(tool_name: ToolName):
    """Build one valid result for each allowed existing Tool output contract."""
    if tool_name is ToolName.SEARCH_KNOWLEDGE:
        document_id = uuid4()
        chunk_id = uuid4()
        return SearchKnowledgeOutput(
            status=ToolStatus.SUCCESS,
            duration_ms=1.5,
            results=[
                SearchKnowledgeResult(
                    chunk_id=chunk_id,
                    document_id=document_id,
                    content="Knowledge excerpt for the active investigation.",
                    service="order-service",
                    environment="local",
                    document_type="runbook",
                    source="knowledge/runbooks/order-errors.md",
                    section="Initial checks",
                    vector_score=0.9,
                    keyword_score=0.8,
                    fusion_score=0.03,
                        citation=CitationOutput(
                            id=f"citation:{chunk_id}",
                            document_id=document_id,
                            chunk_id=chunk_id,
                            document_title="Order error runbook",
                            source="knowledge/runbooks/order-errors.md",
                            source_path="knowledge/runbooks/order-errors.md",
                            chunk_index=0,
                            section="Initial checks",
                            document_version="v1",
                            target_id=TEST_TARGET_ID,
                            scope="service",
                            service_id=TEST_SERVICE_ID,
                            environment="local",
                            document_reference="knowledge/runbooks/order-errors.md#initial-checks",
                        ),
                )
            ],
        )
    if tool_name is ToolName.QUERY_LOGS:
        return QueryLogsOutput(
            status=ToolStatus.SUCCESS,
            duration_ms=2.0,
            provenance=_runtime_provenance(),
            match_count=3,
        )
    if tool_name is ToolName.QUERY_METRICS:
        return QueryMetricsOutput(
            status=ToolStatus.SUCCESS,
            duration_ms=2.5,
            provenance=_runtime_provenance(),
            metrics=MetricSnapshot(
                service="order-service",
                environment="local",
                health_status="ok",
                request_count=10,
                success_count=8,
                error_count=2,
                error_rate=0.2,
                last_request_duration_ms=18.0,
                average_request_duration_ms=12.0,
            ),
        )
    if tool_name is ToolName.QUERY_TRACES:
        return QueryTracesOutput(
            status=ToolStatus.SUCCESS,
            duration_ms=3.0,
            provenance=_runtime_provenance(),
            traces=[],
        )
    if tool_name is ToolName.GET_DEPLOYMENT_HISTORY:
        return GetDeploymentHistoryOutput(
            status=ToolStatus.SUCCESS,
            duration_ms=1.0,
            provenance=_runtime_provenance(),
            deployments=[
                DeploymentRecord(
                    service="order-service",
                    environment="local",
                    current_version="v1.0.0",
                    previous_version="v0.9.0",
                )
            ],
        )
    raise AssertionError(f"unexpected ToolName in test: {tool_name}")


def _runtime_provenance() -> RuntimeEvidenceProvenance:
    return RuntimeEvidenceProvenance(
        source="test_adapter",
        service="order-service",
        environment="local",
        observed_at=datetime(2026, 8, 8, 10, 0, tzinfo=UTC),
    )


def tool_arguments(tool_name: ToolName) -> dict[str, object]:
    """Return a valid argument object for each read-only Tool input schema."""
    if tool_name is ToolName.SEARCH_KNOWLEDGE:
        return {
            "query": "order endpoint errors",
            "target_id": str(TEST_TARGET_ID),
            "service_id": str(TEST_SERVICE_ID),
            "environment": "local",
        }
    if tool_name is ToolName.QUERY_LOGS:
        return {
            "service": "order-service",
            "environment": "local",
            "time_range_start": "2026-08-08T10:00:00+00:00",
            "time_range_end": "2026-08-08T10:05:00+00:00",
        }
    if tool_name is ToolName.QUERY_METRICS:
        return {"service": "order-service", "environment": "local"}
    if tool_name is ToolName.QUERY_TRACES:
        return {
            "service": "order-service",
            "environment": "local",
            "time_range_start": "2026-08-08T10:00:00+00:00",
            "time_range_end": "2026-08-08T10:05:00+00:00",
        }
    if tool_name is ToolName.GET_DEPLOYMENT_HISTORY:
        return {"service": "order-service", "environment": "local"}
    raise AssertionError(f"unexpected ToolName in test: {tool_name}")


@pytest.mark.parametrize(
    "tool_name,executor_name",
    [
        (ToolName.SEARCH_KNOWLEDGE, "search_knowledge"),
        (ToolName.QUERY_LOGS, "query_logs"),
        (ToolName.QUERY_METRICS, "query_metrics"),
        (ToolName.QUERY_TRACES, "query_traces"),
        (ToolName.GET_DEPLOYMENT_HISTORY, "get_deployment_history"),
    ],
)
def test_each_read_only_tool_dispatches_and_records_success(
    monkeypatch, tool_name: ToolName, executor_name: str
) -> None:
    state = build_execution_state(tool_name, tool_arguments(tool_name))
    state["consecutive_failures"] = 2
    calls = 0

    def fake_executor(*_: object):
        nonlocal calls
        calls += 1
        return successful_output(tool_name)

    monkeypatch.setattr(execution_module, executor_name, fake_executor)
    updated = tool_execution_node(state, fake_dependencies())

    assert calls == 1
    assert updated["current_stage"] is AgentStage.HYPOTHESIS_UPDATE
    assert updated["pending_tool_call"] is None
    assert updated["tool_call_count"] == 1
    assert updated["consecutive_failures"] == 0
    assert updated["investigation_round"] == 0
    assert updated["hypotheses"] == state["hypotheses"]
    assert len(updated["evidence"]) == 1
    history = updated["tool_history"][0]
    assert history.tool_name is tool_name
    assert history.status is ToolStatus.SUCCESS
    assert history.duration_ms == successful_output(tool_name).duration_ms
    expected_arguments = tool_registry.get(tool_name).input_model.model_validate(
        tool_arguments(tool_name)
    )
    assert history.tool_arguments == expected_arguments.model_dump(mode="json")
    assert history.evidence_ids == [updated["evidence"][0].id]


def test_search_knowledge_evidence_reuses_retrieval_citation_mapping(monkeypatch) -> None:
    tool_name = ToolName.SEARCH_KNOWLEDGE
    state = build_execution_state(tool_name, tool_arguments(tool_name))
    output = successful_output(tool_name)
    monkeypatch.setattr(execution_module, "search_knowledge", lambda *_: output)

    updated = tool_execution_node(state, fake_dependencies())

    evidence = updated["evidence"][0]
    assert evidence.source == "search_knowledge"
    assert evidence.evidence_type == "knowledge_retrieval"
    assert evidence.reference == output.results[0].citation.document_reference
    assert evidence.data["chunk_id"] == str(output.results[0].chunk_id)


@pytest.mark.parametrize("status", [ToolStatus.FAILURE, ToolStatus.UNAVAILABLE])
def test_failed_tool_records_error_without_evidence_and_returns_to_planning(
    monkeypatch, status: ToolStatus
) -> None:
    tool_name = ToolName.QUERY_METRICS
    state = build_execution_state(tool_name, tool_arguments(tool_name))
    output = QueryMetricsOutput(
        status=status,
        error=ToolError(code="adapter_unavailable", message="adapter unavailable", retryable=True),
        duration_ms=7.0,
    )
    monkeypatch.setattr(execution_module, "query_metrics", lambda *_: output)

    updated = tool_execution_node(state, fake_dependencies())

    assert updated["current_stage"] is AgentStage.INVESTIGATION_PLANNING
    assert updated["pending_tool_call"] is None
    assert updated["evidence"] == []
    assert updated["tool_call_count"] == 1
    assert updated["consecutive_failures"] == 1
    history = updated["tool_history"][0]
    assert history.status is status
    assert history.duration_ms == 7.0
    assert history.error == output.error
    assert history.tool_arguments == {"service": "order-service", "environment": "local"}
    assert history.evidence_ids == []
    assert updated["hypotheses"] == state["hypotheses"]


@pytest.mark.parametrize(
    "error_code", ["timeout", "provider_unavailable", "invalid_provider_response"]
)
def test_retryable_tool_failure_retries_within_the_runtime_budget(
    monkeypatch: pytest.MonkeyPatch, error_code: str
) -> None:
    state = build_execution_state(
        ToolName.QUERY_METRICS, tool_arguments(ToolName.QUERY_METRICS)
    )
    outputs = [
        QueryMetricsOutput(
            status=ToolStatus.FAILURE,
            error=ToolError(code=error_code, message="safe failure", retryable=True),
        ),
        successful_output(ToolName.QUERY_METRICS),
    ]
    monkeypatch.setattr(execution_module, "query_metrics", lambda *_: outputs.pop(0))
    budget = InvestigationBudget(max_workflow_retries=1)

    retrying = tool_execution_node(state, fake_dependencies(), budget=budget)
    recovered = tool_execution_node(retrying, fake_dependencies(), budget=budget)

    assert retrying["tool_call_count"] == 1
    assert retrying["retry_count"] == 1
    assert retrying["retry_pending"] is True
    assert retrying["consecutive_failures"] == 1
    assert retrying["pending_tool_call"] is not None
    assert retrying["current_stage"] is AgentStage.TOOL_EXECUTION
    assert recovered["tool_call_count"] == 2
    assert recovered["retry_count"] == 0
    assert recovered["retry_pending"] is False
    assert recovered["consecutive_failures"] == 0
    assert recovered["evidence"]


def test_successful_retry_resets_the_budget_for_a_new_independent_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = build_execution_state(
        ToolName.QUERY_METRICS, tool_arguments(ToolName.QUERY_METRICS)
    )
    outputs = [
        QueryMetricsOutput(
            status=ToolStatus.FAILURE,
            error=ToolError(code="timeout", message="safe timeout", retryable=True),
        ),
        successful_output(ToolName.QUERY_METRICS),
        QueryMetricsOutput(
            status=ToolStatus.FAILURE,
            error=ToolError(code="timeout", message="safe timeout", retryable=True),
        ),
    ]
    monkeypatch.setattr(execution_module, "query_metrics", lambda *_: outputs.pop(0))
    budget = InvestigationBudget(max_workflow_retries=1)

    retrying = tool_execution_node(state, fake_dependencies(), budget=budget)
    recovered = tool_execution_node(retrying, fake_dependencies(), budget=budget)
    assert recovered["retry_count"] == 0
    assert recovered["retry_pending"] is False

    next_state = build_execution_state(
        ToolName.QUERY_METRICS, tool_arguments(ToolName.QUERY_METRICS)
    )
    next_state.update(
        {
            "tool_history": recovered["tool_history"],
            "tool_call_count": recovered["tool_call_count"],
            "evidence": recovered["evidence"],
            "retry_count": recovered["retry_count"],
            "retry_pending": recovered["retry_pending"],
        }
    )
    new_retry = tool_execution_node(next_state, fake_dependencies(), budget=budget)

    assert new_retry["retry_count"] == 1
    assert new_retry["retry_pending"] is True
    assert new_retry["pending_tool_call"] is not None


def test_retryable_tool_failure_exhaustion_fails_without_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = build_execution_state(
        ToolName.QUERY_METRICS, tool_arguments(ToolName.QUERY_METRICS)
    )
    failure = QueryMetricsOutput(
        status=ToolStatus.FAILURE,
        error=ToolError(code="timeout", message="safe timeout", retryable=True),
    )
    monkeypatch.setattr(execution_module, "query_metrics", lambda *_: failure)
    budget = InvestigationBudget(max_workflow_retries=1)

    retrying = tool_execution_node(state, fake_dependencies(), budget=budget)
    exhausted = tool_execution_node(retrying, fake_dependencies(), budget=budget)

    assert exhausted["tool_call_count"] == 2
    assert exhausted["retry_count"] == 1
    assert exhausted["evidence"] == []
    assert exhausted["terminal_reason"].value == "retry_budget_exhausted"
    assert exhausted["workflow_failure_category"] is not None


@pytest.mark.parametrize("error_code", ["invalid_request", "capability_unavailable"])
def test_nonretryable_tool_failure_falls_back_without_failure_streak(
    monkeypatch: pytest.MonkeyPatch, error_code: str
) -> None:
    state = build_execution_state(
        ToolName.QUERY_METRICS, tool_arguments(ToolName.QUERY_METRICS)
    )
    output = QueryMetricsOutput(
        status=ToolStatus.FAILURE,
        error=ToolError(code=error_code, message="safe request failure"),
    )
    monkeypatch.setattr(execution_module, "query_metrics", lambda *_: output)

    updated = tool_execution_node(
        state, fake_dependencies(), budget=InvestigationBudget(max_workflow_retries=3)
    )

    assert updated["current_stage"] is AgentStage.INVESTIGATION_PLANNING
    assert updated["pending_tool_call"] is None
    assert updated["retry_count"] == 0
    assert updated["consecutive_failures"] == 0
    assert updated["last_failure_category"].value == error_code
    assert updated["evidence"] == []


def test_disabled_capability_does_not_increment_the_execution_failure_streak() -> None:
    state = build_execution_state(
        ToolName.QUERY_METRICS, tool_arguments(ToolName.QUERY_METRICS)
    )
    dependencies = ToolExecutionDependencies(  # type: ignore[arg-type]
        rag_service=object(),
        logs_adapter=None,
        metrics_adapter=None,
        traces_adapter=None,
        deployment_adapter=None,
        available_tools=frozenset(),
    )

    updated = tool_execution_node(
        state, dependencies, budget=InvestigationBudget(max_workflow_retries=3)
    )

    assert updated["retry_count"] == 0
    assert updated["consecutive_failures"] == 0
    assert updated["last_failure_category"].value == "capability_unavailable"
    assert updated["tool_history"][0].status is ToolStatus.UNAVAILABLE


def test_raw_provider_exception_is_replaced_with_a_safe_retryable_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = build_execution_state(
        ToolName.QUERY_METRICS, tool_arguments(ToolName.QUERY_METRICS)
    )
    monkeypatch.setattr(
        execution_module,
        "query_metrics",
        lambda *_: (_ for _ in ()).throw(RuntimeError("token=top-secret")),
    )

    updated = tool_execution_node(
        state, fake_dependencies(), budget=InvestigationBudget(max_workflow_retries=1)
    )

    history_error = updated["tool_history"][0].error
    assert history_error is not None
    assert history_error.code == "provider_unavailable"
    assert "top-secret" not in history_error.message
    assert "top-secret" not in str(updated)


def test_executor_rejects_tampered_rollback_before_dispatch(monkeypatch) -> None:
    state = build_execution_state(
        ToolName.ROLLBACK_DEPLOYMENT,
        {
            "service": "order-service",
            "environment": "local",
            "target_version": "v1.0.0",
            "reason": "not allowed",
            "approval_id": str(uuid4()),
        },
    )
    calls = 0

    def rollback_guard(*_: object) -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(execution_module, "get_deployment_history", rollback_guard)

    with pytest.raises(ToolExecutionError, match="disallowed tool"):
        tool_execution_node(state, fake_dependencies())

    assert calls == 0
    assert state["pending_tool_call"] is not None
    assert state["tool_history"] == []
    assert state["tool_call_count"] == 0


def test_executor_skips_without_pending_call_or_outside_execution_stage() -> None:
    state = build_execution_state(ToolName.QUERY_METRICS, tool_arguments(ToolName.QUERY_METRICS))
    state["pending_tool_call"] = None

    skipped_without_pending = tool_execution_node(state, fake_dependencies())
    state["pending_tool_call"] = PendingToolCall(
        investigation_goal="Collect one metric snapshot.",
        tool_name=ToolName.QUERY_METRICS,
        tool_arguments=tool_arguments(ToolName.QUERY_METRICS),
        reason="A metric snapshot is relevant.",
    )
    state["current_stage"] = AgentStage.INVESTIGATION_PLANNING
    skipped_for_stage = tool_execution_node(state, fake_dependencies())

    assert skipped_without_pending is state
    assert skipped_for_stage is state


def test_executor_revalidates_tampered_pending_arguments_before_dispatch(monkeypatch) -> None:
    state = build_execution_state(ToolName.QUERY_LOGS, tool_arguments(ToolName.QUERY_LOGS))
    assert state["pending_tool_call"] is not None
    state["pending_tool_call"].tool_arguments = {"service": "order-service"}
    calls = 0

    def fake_query_logs(*_: object) -> QueryLogsOutput:
        nonlocal calls
        calls += 1
        return QueryLogsOutput(status=ToolStatus.SUCCESS)

    monkeypatch.setattr(execution_module, "query_logs", fake_query_logs)

    updated = tool_execution_node(state, fake_dependencies())

    assert calls == 0
    assert updated["tool_call_count"] == 1
    assert updated["consecutive_failures"] == 0
    assert updated["last_failure_category"].value == "invalid_request"
    assert updated["tool_history"][0].error is not None
    assert updated["tool_history"][0].error.code == "invalid_request"
