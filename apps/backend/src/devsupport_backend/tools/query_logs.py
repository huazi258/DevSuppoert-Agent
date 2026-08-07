"""Structured Tool executor for bounded Fault Lab log investigation."""

from collections import Counter
from time import perf_counter

from devsupport_backend.tools.logs import FaultLabLogsAdapter, LogsAdapterError
from devsupport_backend.tools.schemas import (
    ErrorPattern,
    LogSample,
    QueryLogsInput,
    QueryLogsOutput,
    ToolError,
    ToolStatus,
)


def query_logs(
    tool_input: QueryLogsInput,
    logs_adapter: FaultLabLogsAdapter,
) -> QueryLogsOutput:
    """Call the fixed logs adapter and summarize only bounded structured records."""
    started_at = perf_counter()
    try:
        result = logs_adapter.query(tool_input)
    except LogsAdapterError as error:
        return QueryLogsOutput(
            status=ToolStatus.FAILURE,
            error=ToolError(code=error.code, message=str(error), retryable=error.retryable),
            duration_ms=_duration_ms(started_at),
        )

    samples = [
        LogSample(
            timestamp=event.timestamp,
            service=event.service,
            level=event.level,
            message=event.message,
            request_id=event.request_id,
            trace_id=event.trace_id,
            error_type=event.error_type,
            status_code=event.status_code,
            duration_ms=event.duration_ms,
            downstream_service=event.downstream_service,
        )
        for event in result.events
    ]
    error_patterns = Counter(
        event.error_type or event.message for event in result.events if event.level == "error"
    )
    trace_ids = list(dict.fromkeys(event.trace_id for event in result.events if event.trace_id))
    timestamps = [event.timestamp for event in result.events]
    return QueryLogsOutput(
        status=ToolStatus.SUCCESS,
        duration_ms=_duration_ms(started_at),
        match_count=result.match_count,
        first_seen=min(timestamps) if timestamps else None,
        last_seen=max(timestamps) if timestamps else None,
        error_patterns=[
            ErrorPattern(pattern=pattern, count=count)
            for pattern, count in sorted(
                error_patterns.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        samples=samples,
        trace_ids=trace_ids,
    )


def _duration_ms(started_at: float) -> float:
    """Return a non-negative elapsed duration for the future ToolCall audit."""
    return max(0.0, round((perf_counter() - started_at) * 1_000, 2))
