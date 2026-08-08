"""Tests for safe dispatch and compact evidence from existing read-only Tools."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

import devsupport_backend.agent.nodes.tool_execution as execution_module
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
    SearchKnowledgeOutput,
    SearchKnowledgeResult,
    ToolError,
    ToolStatus,
)


def build_execution_state(tool_name: ToolName, arguments: dict[str, object]) -> AgentState:
    """Create a planning-approved pending call without contacting external services."""
    started_at = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
    incident = Incident(
        id=uuid4(),
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
                        source="knowledge/runbooks/order-errors.md",
                        section="Initial checks",
                        document_reference="knowledge/runbooks/order-errors.md#initial-checks",
                    ),
                )
            ],
        )
    if tool_name is ToolName.QUERY_LOGS:
        return QueryLogsOutput(status=ToolStatus.SUCCESS, duration_ms=2.0, match_count=3)
    if tool_name is ToolName.QUERY_METRICS:
        return QueryMetricsOutput(
            status=ToolStatus.SUCCESS,
            duration_ms=2.5,
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
        return QueryTracesOutput(status=ToolStatus.SUCCESS, duration_ms=3.0, traces=[])
    if tool_name is ToolName.GET_DEPLOYMENT_HISTORY:
        return GetDeploymentHistoryOutput(
            status=ToolStatus.SUCCESS,
            duration_ms=1.0,
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


def tool_arguments(tool_name: ToolName) -> dict[str, object]:
    """Return a valid argument object for each read-only Tool input schema."""
    if tool_name is ToolName.SEARCH_KNOWLEDGE:
        return {"query": "order endpoint errors", "service": "order-service"}
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
    history = updated["tool_history"][0]
    assert history.status is status
    assert history.duration_ms == 7.0
    assert history.error == output.error
    assert history.tool_arguments == {"service": "order-service", "environment": "local"}
    assert history.evidence_ids == []
    assert updated["hypotheses"] == state["hypotheses"]


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

    with pytest.raises(ToolExecutionError, match="pending tool arguments are invalid"):
        tool_execution_node(state, fake_dependencies())

    assert calls == 0
    assert state["tool_history"] == []
    assert state["tool_call_count"] == 0
