"""Safe dispatch of one pending read-only investigation Tool call."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from pydantic import BaseModel, ValidationError

from devsupport_backend.agent.nodes.retrieval import _append_unique_evidence
from devsupport_backend.agent.state import AgentStage, AgentState, EvidenceContext, ToolHistoryEntry
from devsupport_backend.rag.retrieval import RAGService
from devsupport_backend.tools.deployments import FaultLabDeploymentAdapter
from devsupport_backend.tools.get_deployment_history import get_deployment_history
from devsupport_backend.tools.logs import FaultLabLogsAdapter
from devsupport_backend.tools.metrics import FaultLabMetricsAdapter
from devsupport_backend.tools.query_logs import query_logs
from devsupport_backend.tools.query_metrics import query_metrics
from devsupport_backend.tools.query_traces import query_traces
from devsupport_backend.tools.registry import ToolName, tool_registry
from devsupport_backend.tools.schemas import (
    GetDeploymentHistoryInput,
    GetDeploymentHistoryOutput,
    QueryLogsInput,
    QueryLogsOutput,
    QueryMetricsInput,
    QueryMetricsOutput,
    QueryTracesInput,
    QueryTracesOutput,
    SearchKnowledgeInput,
    SearchKnowledgeOutput,
    ToolOutput,
    ToolStatus,
    TraceSpan,
)
from devsupport_backend.tools.search_knowledge import search_knowledge
from devsupport_backend.tools.traces import FaultLabTracesAdapter

MAX_LOG_ERROR_PATTERNS = 10
MAX_LOG_TRACE_IDS = 20
MAX_TRACE_SUMMARIES = 5
MAX_TRACE_ERRORS = 2
MAX_DEPLOYMENT_RECORDS = 10
MAX_RUNTIME_EVIDENCE_TEXT_CHARS = 250
"""Bounds for concise runtime evidence retained after one Tool call."""

READ_ONLY_INVESTIGATION_TOOLS = frozenset(
    {
        ToolName.SEARCH_KNOWLEDGE,
        ToolName.QUERY_LOGS,
        ToolName.QUERY_METRICS,
        ToolName.QUERY_TRACES,
        ToolName.GET_DEPLOYMENT_HISTORY,
    }
)
"""Execution-time allowlist, repeated independently from Planner validation."""


class ToolExecutionError(RuntimeError):
    """Raised when a pending call fails the executor's defensive safety checks."""


@dataclass(frozen=True)
class ToolExecutionDependencies:
    """Existing Tool dependencies; this node owns no external-call implementation."""

    rag_service: RAGService
    logs_adapter: FaultLabLogsAdapter
    metrics_adapter: FaultLabMetricsAdapter
    traces_adapter: FaultLabTracesAdapter
    deployment_adapter: FaultLabDeploymentAdapter


def tool_execution_node(state: AgentState, dependencies: ToolExecutionDependencies) -> AgentState:
    """Execute one revalidated read-only Tool and record compact structured facts."""
    pending_tool_call = state["pending_tool_call"]
    if pending_tool_call is None or state["current_stage"] != AgentStage.TOOL_EXECUTION:
        return state

    if pending_tool_call.tool_name not in READ_ONLY_INVESTIGATION_TOOLS:
        raise ToolExecutionError(
            f"execution rejected disallowed tool: {pending_tool_call.tool_name}"
        )

    validated_input = _validate_pending_arguments(
        pending_tool_call.tool_name,
        pending_tool_call.tool_arguments,
    )
    tool_output = _dispatch(pending_tool_call.tool_name, validated_input, dependencies)
    tool_history_entry = ToolHistoryEntry(
        tool_name=pending_tool_call.tool_name,
        tool_arguments=validated_input.model_dump(mode="json"),
        status=tool_output.status,
        duration_ms=tool_output.duration_ms,
        error=tool_output.error,
    )

    if tool_output.status is not ToolStatus.SUCCESS:
        return {
            **state,
            "tool_history": [*state["tool_history"], tool_history_entry],
            "tool_call_count": state["tool_call_count"] + 1,
            "pending_tool_call": None,
            "current_stage": AgentStage.INVESTIGATION_PLANNING,
        }

    evidence, evidence_ids = _to_evidence(state, pending_tool_call.tool_name, tool_output)
    tool_history_entry = tool_history_entry.model_copy(update={"evidence_ids": evidence_ids})
    return {
        **state,
        "evidence": evidence,
        "tool_history": [*state["tool_history"], tool_history_entry],
        "tool_call_count": state["tool_call_count"] + 1,
        "pending_tool_call": None,
        "current_stage": AgentStage.HYPOTHESIS_UPDATE,
    }


def _validate_pending_arguments(tool_name: ToolName, arguments: dict[str, object]) -> BaseModel:
    """Revalidate persisted planner arguments against the registered input contract."""
    definition = tool_registry.get(tool_name)
    try:
        return definition.input_model.model_validate(arguments)
    except ValidationError as error:
        raise ToolExecutionError(f"pending tool arguments are invalid: {error}") from error


def _dispatch(
    tool_name: ToolName, tool_input: BaseModel, dependencies: ToolExecutionDependencies
) -> ToolOutput:
    """Call only explicit existing Tool implementations; no dynamic execution is allowed."""
    if tool_name is ToolName.SEARCH_KNOWLEDGE:
        return search_knowledge(cast(SearchKnowledgeInput, tool_input), dependencies.rag_service)
    if tool_name is ToolName.QUERY_LOGS:
        return query_logs(cast(QueryLogsInput, tool_input), dependencies.logs_adapter)
    if tool_name is ToolName.QUERY_METRICS:
        return query_metrics(cast(QueryMetricsInput, tool_input), dependencies.metrics_adapter)
    if tool_name is ToolName.QUERY_TRACES:
        return query_traces(cast(QueryTracesInput, tool_input), dependencies.traces_adapter)
    if tool_name is ToolName.GET_DEPLOYMENT_HISTORY:
        return get_deployment_history(
            cast(GetDeploymentHistoryInput, tool_input), dependencies.deployment_adapter
        )
    raise ToolExecutionError(f"execution rejected unhandled tool: {tool_name}")


def _to_evidence(
    state: AgentState, tool_name: ToolName, output: ToolOutput
) -> tuple[list[EvidenceContext], list[UUID]]:
    """Map successful bounded Tool results to concise evidence records."""
    if tool_name is ToolName.SEARCH_KNOWLEDGE:
        search_output = cast(SearchKnowledgeOutput, output)
        return _append_unique_evidence(state, search_output.results)

    evidence = [*state["evidence"]]
    item = _runtime_evidence(tool_name, output)
    evidence.append(item)
    return evidence, [item.id]


def _runtime_evidence(tool_name: ToolName, output: ToolOutput) -> EvidenceContext:
    """Keep only concise facts that a later hypothesis update can interpret."""
    if tool_name is ToolName.QUERY_LOGS:
        logs_output = cast(QueryLogsOutput, output)
        patterns = [
            {"pattern": _truncate(item.pattern), "count": item.count}
            for item in logs_output.error_patterns[:MAX_LOG_ERROR_PATTERNS]
        ]
        return EvidenceContext(
            evidence_type="log_query_result",
            source=tool_name.value,
            summary=f"Log query matched {logs_output.match_count} event(s).",
            data={
                "match_count": logs_output.match_count,
                "first_seen": _serialize_time(logs_output.first_seen),
                "last_seen": _serialize_time(logs_output.last_seen),
                "error_patterns": patterns,
                "trace_ids": [
                    _truncate(trace_id)
                    for trace_id in logs_output.trace_ids[:MAX_LOG_TRACE_IDS]
                ],
                "sample_count": len(logs_output.samples),
            },
        )
    if tool_name is ToolName.QUERY_METRICS:
        metrics_output = cast(QueryMetricsOutput, output)
        return EvidenceContext(
            evidence_type="metric_snapshot",
            source=tool_name.value,
            summary="Metric snapshot returned current request, error, latency, and health facts.",
            data={
                "metrics": (
                    metrics_output.metrics.model_dump(mode="json")
                    if metrics_output.metrics
                    else None
                )
            },
        )
    if tool_name is ToolName.QUERY_TRACES:
        traces_output = cast(QueryTracesOutput, output)
        traces = [
            {
                "trace_id": trace.trace_id,
                "duration_ms": trace.duration_ms,
                "status": trace.status,
                "slowest_span": _compact_trace_span(trace.slowest_span),
                "errors": [
                    {
                        "service": error.service,
                        "span_id": _truncate(error.span_id),
                        "operation": _truncate(error.operation),
                        "message": _truncate(error.message),
                    }
                    for error in trace.errors[:MAX_TRACE_ERRORS]
                ],
            }
            for trace in traces_output.traces[:MAX_TRACE_SUMMARIES]
        ]
        return EvidenceContext(
            evidence_type="trace_query_result",
            source=tool_name.value,
            summary=f"Trace query returned {len(traces_output.traces)} trace summary record(s).",
            data={"trace_count": len(traces_output.traces), "traces": traces},
        )
    if tool_name is ToolName.GET_DEPLOYMENT_HISTORY:
        deployments_output = cast(GetDeploymentHistoryOutput, output)
        return EvidenceContext(
            evidence_type="deployment_facts",
            source=tool_name.value,
            summary=f"Deployment query returned {len(deployments_output.deployments)} record(s).",
            data={
                "deployments": [
                    item.model_dump(mode="json")
                    for item in deployments_output.deployments[:MAX_DEPLOYMENT_RECORDS]
                ]
            },
        )
    raise ToolExecutionError(f"execution cannot map evidence for tool: {tool_name}")


def _serialize_time(value: object) -> str | None:
    """Return only JSON-safe time facts from an optional runtime Tool response."""
    return value.isoformat() if hasattr(value, "isoformat") else None


def _compact_trace_span(span: TraceSpan | None) -> dict[str, object] | None:
    """Keep only one bounded slow-span fact per returned trace."""
    if span is None:
        return None
    return {
        "service": span.service,
        "operation": _truncate(span.operation),
        "duration_ms": span.duration_ms,
        "status": span.status,
        "error": _truncate(span.error) if span.error else None,
    }


def _truncate(value: str) -> str:
    """Bound string facts so runtime evidence cannot become a raw response dump."""
    return value[:MAX_RUNTIME_EVIDENCE_TEXT_CHARS]
