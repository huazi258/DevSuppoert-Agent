"""Safe dispatch of one pending read-only investigation Tool call."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from pydantic import BaseModel, ValidationError

from devsupport_backend.agent.budget import InvestigationBudget
from devsupport_backend.agent.nodes.retrieval import _append_unique_evidence
from devsupport_backend.agent.retry_policy import (
    classify_tool_error,
    counts_as_execution_failure,
    retry_decision,
)
from devsupport_backend.agent.state import (
    AgentStage,
    AgentState,
    EvidenceContext,
    FailureCategory,
    PendingToolCall,
    RuntimeFailureCategory,
    TerminalReason,
    ToolHistoryEntry,
)
from devsupport_backend.rag.retrieval import RAGService
from devsupport_backend.tools.adapter_contracts import (
    DeploymentAdapter,
    LogsAdapter,
    MetricsAdapter,
    TracesAdapter,
)
from devsupport_backend.tools.get_deployment_history import get_deployment_history
from devsupport_backend.tools.query_logs import query_logs
from devsupport_backend.tools.query_metrics import query_metrics
from devsupport_backend.tools.query_traces import query_traces
from devsupport_backend.tools.registry import (
    V2_READ_ONLY_TOOL_NAMES,
    ToolName,
    v2_tool_registry,
)
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
    ToolError,
    ToolOutput,
    ToolStatus,
    TraceSpan,
)
from devsupport_backend.tools.search_knowledge import search_knowledge

MAX_LOG_ERROR_PATTERNS = 10
MAX_LOG_TRACE_IDS = 20
MAX_TRACE_SUMMARIES = 5
MAX_TRACE_ERRORS = 2
MAX_DEPLOYMENT_RECORDS = 10
MAX_RUNTIME_EVIDENCE_TEXT_CHARS = 250
"""Bounds for concise runtime evidence retained after one Tool call."""

READ_ONLY_INVESTIGATION_TOOLS = V2_READ_ONLY_TOOL_NAMES
"""Execution-time allowlist, repeated independently from Planner validation."""


class ToolExecutionError(RuntimeError):
    """Raised when a pending call fails the executor's defensive safety checks."""


@dataclass(frozen=True)
class ToolExecutionDependencies:
    """Existing Tool dependencies; this node owns no external-call implementation."""

    rag_service: RAGService
    logs_adapter: LogsAdapter
    metrics_adapter: MetricsAdapter
    traces_adapter: TracesAdapter | None
    deployment_adapter: DeploymentAdapter | None
    available_tools: frozenset[ToolName] = READ_ONLY_INVESTIGATION_TOOLS

    def __post_init__(self) -> None:
        if not self.available_tools.issubset(READ_ONLY_INVESTIGATION_TOOLS):
            raise ValueError("V2 ToolExecutionDependencies only accepts read-only tools")
        if ToolName.QUERY_LOGS in self.available_tools and self.logs_adapter is None:
            raise ValueError("query_logs requires a configured logs adapter")
        if ToolName.QUERY_METRICS in self.available_tools and self.metrics_adapter is None:
            raise ValueError("query_metrics requires a configured metrics adapter")


def tool_execution_node(
    state: AgentState,
    dependencies: ToolExecutionDependencies,
    *,
    budget: InvestigationBudget | None = None,
) -> AgentState:
    """Execute one revalidated read-only Tool and record compact structured facts."""
    pending_tool_call = state["pending_tool_call"]
    if pending_tool_call is None or state["current_stage"] != AgentStage.TOOL_EXECUTION:
        return state

    if pending_tool_call.tool_name not in READ_ONLY_INVESTIGATION_TOOLS:
        raise ToolExecutionError(
            f"execution rejected disallowed tool: {pending_tool_call.tool_name}"
        )
    if pending_tool_call.tool_name not in dependencies.available_tools:
        return _capability_unavailable_state(state, pending_tool_call)

    try:
        validated_input = _validate_pending_arguments(
            pending_tool_call.tool_name,
            pending_tool_call.tool_arguments,
            state,
        )
    except ToolExecutionError:
        return _invalid_request_state(state, pending_tool_call)
    try:
        tool_output = _dispatch(pending_tool_call.tool_name, validated_input, dependencies)
    except Exception:
        return _provider_exception_state(state, pending_tool_call, budget)
    tool_history_entry = ToolHistoryEntry(
        tool_name=pending_tool_call.tool_name,
        tool_arguments=validated_input.model_dump(mode="json"),
        status=tool_output.status,
        duration_ms=tool_output.duration_ms,
        error=tool_output.error,
    )

    if tool_output.status is not ToolStatus.SUCCESS:
        return _tool_failure_state(state, pending_tool_call, tool_history_entry, budget)

    evidence, evidence_ids = _to_evidence(state, pending_tool_call.tool_name, tool_output)
    tool_history_entry = tool_history_entry.model_copy(update={"evidence_ids": evidence_ids})
    return {
        **state,
        "evidence": evidence,
        "tool_history": [*state["tool_history"], tool_history_entry],
        "tool_call_count": state["tool_call_count"] + 1,
        "consecutive_failures": 0,
        "retry_pending": False,
        "pending_tool_call": None,
        "current_stage": AgentStage.HYPOTHESIS_UPDATE,
    }


def _capability_unavailable_state(
    state: AgentState, pending_tool_call: PendingToolCall
) -> AgentState:
    """Fail closed for a persisted call that is no longer enabled by this composition."""
    entry = ToolHistoryEntry(
        tool_name=pending_tool_call.tool_name,
        tool_arguments=pending_tool_call.tool_arguments,
        status=ToolStatus.UNAVAILABLE,
        error=ToolError(
            code="capability_unavailable",
            message="The requested investigation capability is not enabled.",
        ),
    )
    return _record_nonretryable_tool_failure(
        state,
        pending_tool_call,
        entry,
        RuntimeFailureCategory.CAPABILITY_UNAVAILABLE,
    )


def _invalid_request_state(state: AgentState, pending_tool_call: PendingToolCall) -> AgentState:
    """Record a rejected request without retaining unvalidated planner input."""
    entry = ToolHistoryEntry(
        tool_name=pending_tool_call.tool_name,
        tool_arguments=_safe_pending_arguments(pending_tool_call),
        status=ToolStatus.FAILURE,
        error=ToolError(
            code=RuntimeFailureCategory.INVALID_REQUEST.value,
            message="The investigation request is invalid.",
        ),
    )
    return _record_nonretryable_tool_failure(
        state,
        pending_tool_call,
        entry,
        RuntimeFailureCategory.INVALID_REQUEST,
    )


def _provider_exception_state(
    state: AgentState,
    pending_tool_call: PendingToolCall,
    budget: InvestigationBudget | None,
) -> AgentState:
    """Convert an unexpected provider exception into a bounded safe failure record."""
    entry = ToolHistoryEntry(
        tool_name=pending_tool_call.tool_name,
        tool_arguments=_safe_pending_arguments(pending_tool_call),
        status=ToolStatus.FAILURE,
        error=ToolError(
            code=RuntimeFailureCategory.PROVIDER_UNAVAILABLE.value,
            message="The runtime evidence provider is unavailable.",
            retryable=True,
        ),
    )
    return _tool_failure_state(state, pending_tool_call, entry, budget)


def _tool_failure_state(
    state: AgentState,
    pending_tool_call: PendingToolCall,
    entry: ToolHistoryEntry,
    budget: InvestigationBudget | None,
) -> AgentState:
    """Record one Tool failure, then retry, fall back, or fail deterministically."""
    assert entry.error is not None
    category = classify_tool_error(entry.error)
    next_tool_call_count = state["tool_call_count"] + 1
    base_state = {
        **state,
        "tool_history": [*state["tool_history"], entry],
        "tool_call_count": next_tool_call_count,
        "consecutive_failures": (
            state.get("consecutive_failures", 0) + 1
            if counts_as_execution_failure(category)
            else state.get("consecutive_failures", 0)
        ),
        "last_failure_category": category,
        "last_failed_tool": pending_tool_call.tool_name,
        "last_failed_tool_arguments": entry.tool_arguments,
        "pending_tool_call": None,
        "current_stage": AgentStage.INVESTIGATION_PLANNING,
    }
    if budget is None:
        return base_state
    if (
        counts_as_execution_failure(category)
        and base_state["consecutive_failures"] >= budget.max_consecutive_failures
    ):
        return {
            **base_state,
            "workflow_failure_category": FailureCategory.TOOL_FAILURE,
            "workflow_failure_retryable": False,
            "workflow_failure_safe_message": "调查工具连续执行失败，已停止本轮调查。",
            "terminal_reason": TerminalReason.REPEATED_FAILURES,
        }
    decision = retry_decision(
        state,
        budget,
        category,
        next_tool_call_count=next_tool_call_count,
    )
    if decision.retry:
        return {
            **base_state,
            "retry_count": state.get("retry_count", 0) + 1,
            "pending_tool_call": pending_tool_call,
            "current_stage": AgentStage.TOOL_EXECUTION,
        }
    if decision.exhausted:
        return {
            **base_state,
            "workflow_failure_category": FailureCategory.TOOL_FAILURE,
            "workflow_failure_retryable": False,
            "workflow_failure_safe_message": "调查工具重试次数已耗尽。",
            "terminal_reason": TerminalReason.RETRY_BUDGET_EXHAUSTED,
        }
    return base_state


def _record_nonretryable_tool_failure(
    state: AgentState,
    pending_tool_call: PendingToolCall,
    entry: ToolHistoryEntry,
    category: RuntimeFailureCategory,
) -> AgentState:
    """Fall back to another legal Tool without inflating the execution failure streak."""
    return {
        **state,
        "tool_history": [*state["tool_history"], entry],
        "tool_call_count": state["tool_call_count"] + 1,
        "last_failure_category": category,
        "last_failed_tool": pending_tool_call.tool_name,
        "last_failed_tool_arguments": entry.tool_arguments,
        "pending_tool_call": None,
        "current_stage": AgentStage.INVESTIGATION_PLANNING,
    }


def _safe_pending_arguments(pending_tool_call: PendingToolCall) -> dict[str, object]:
    """Retain only registered input keys after validation fails, excluding arbitrary payloads."""
    input_model = v2_tool_registry.get(pending_tool_call.tool_name).input_model
    return {
        key: value
        for key, value in pending_tool_call.tool_arguments.items()
        if key in input_model.model_fields
    }


def _validate_pending_arguments(
    tool_name: ToolName, arguments: dict[str, object], state: AgentState
) -> BaseModel:
    """Revalidate persisted planner arguments against the registered input contract."""
    definition = v2_tool_registry.get(tool_name)
    try:
        validated_input = definition.input_model.model_validate(arguments)
    except ValidationError as error:
        raise ToolExecutionError(f"pending tool arguments are invalid: {error}") from error
    if tool_name is ToolName.SEARCH_KNOWLEDGE:
        _require_incident_knowledge_scope(state, validated_input)
    return validated_input


def _require_incident_knowledge_scope(
    state: AgentState, arguments: SearchKnowledgeInput
) -> None:
    """Keep persisted knowledge calls bound to the current Incident's scope."""
    incident = state["incident"]
    if incident.target_id is None or incident.service_id is None:
        raise ToolExecutionError("active Incident is missing its required knowledge scope")
    if (
        arguments.target_id != incident.target_id
        or arguments.service_id != incident.service_id
        or arguments.environment != incident.environment
    ):
        raise ToolExecutionError("pending knowledge scope does not match the active Incident")


def _dispatch(
    tool_name: ToolName, tool_input: BaseModel, dependencies: ToolExecutionDependencies
) -> ToolOutput:
    """Call only explicit existing Tool implementations; no dynamic execution is allowed."""
    if tool_name is ToolName.SEARCH_KNOWLEDGE:
        return search_knowledge(cast(SearchKnowledgeInput, tool_input), dependencies.rag_service)
    if tool_name is ToolName.QUERY_LOGS:
        if dependencies.logs_adapter is None:
            raise ToolExecutionError("logs adapter is unavailable for this composition")
        return query_logs(cast(QueryLogsInput, tool_input), dependencies.logs_adapter)
    if tool_name is ToolName.QUERY_METRICS:
        if dependencies.metrics_adapter is None:
            raise ToolExecutionError("metrics adapter is unavailable for this composition")
        return query_metrics(cast(QueryMetricsInput, tool_input), dependencies.metrics_adapter)
    if tool_name is ToolName.QUERY_TRACES:
        if dependencies.traces_adapter is None:
            raise ToolExecutionError("traces adapter is unavailable for this composition")
        return query_traces(cast(QueryTracesInput, tool_input), dependencies.traces_adapter)
    if tool_name is ToolName.GET_DEPLOYMENT_HISTORY:
        if dependencies.deployment_adapter is None:
            raise ToolExecutionError("deployment adapter is unavailable for this composition")
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
                "provenance": _provenance_data(logs_output),
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
                "provenance": _provenance_data(metrics_output),
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
            data={
                "provenance": _provenance_data(traces_output),
                "trace_count": len(traces_output.traces),
                "traces": traces,
            },
        )
    if tool_name is ToolName.GET_DEPLOYMENT_HISTORY:
        deployments_output = cast(GetDeploymentHistoryOutput, output)
        return EvidenceContext(
            evidence_type="deployment_facts",
            source=tool_name.value,
            summary=f"Deployment query returned {len(deployments_output.deployments)} record(s).",
            data={
                "provenance": _provenance_data(deployments_output),
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


def _provenance_data(output: ToolOutput) -> dict[str, object] | None:
    """Retain safe provider identity and scope without exposing backend configuration."""
    return output.provenance.model_dump(mode="json") if output.provenance else None


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
