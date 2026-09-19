"""Structured Tool executor for bounded log investigation."""

from collections import Counter
from time import perf_counter

from devsupport_backend.tools.adapter_contracts import (
    MAX_NORMALIZED_LOG_EVENTS,
    MAX_NORMALIZED_TEXT_CHARS,
    AdapterError,
    LogsAdapter,
    normalize_adapter_error,
)
from devsupport_backend.tools.schemas import (
    ErrorPattern,
    LogSample,
    QueryLogsInput,
    QueryLogsOutput,
    RuntimeEvidenceProvenance,
    ToolError,
    ToolStatus,
)


def query_logs(
    tool_input: QueryLogsInput,
    logs_adapter: LogsAdapter,
) -> QueryLogsOutput:
    """Call the fixed logs adapter and summarize only bounded structured records."""
    started_at = perf_counter()
    try:
        result = logs_adapter.query(tool_input)
    except AdapterError as error:
        normalized_error = normalize_adapter_error(error)
        return QueryLogsOutput(
            status=ToolStatus.FAILURE,
            error=ToolError(**normalized_error.__dict__),
            duration_ms=_duration_ms(started_at),
        )

    events = result.events[: min(tool_input.limit, MAX_NORMALIZED_LOG_EVENTS)]
    samples = [
        LogSample(
            timestamp=event.timestamp,
            service=event.service,
            level=event.level,
            message=_bounded_text(event.message),
            request_id=_bounded_optional_text(event.request_id, 256),
            trace_id=_bounded_optional_text(event.trace_id, 256),
            error_type=_bounded_optional_text(event.error_type, 256),
            status_code=event.status_code,
            duration_ms=event.duration_ms,
            downstream_service=_bounded_optional_text(event.downstream_service, 100),
        )
        for event in events
    ]
    error_patterns = Counter(
        _bounded_text(event.error_type or event.message)
        for event in events
        if event.level == "error"
    )
    trace_ids = list(
        dict.fromkeys(
            _bounded_optional_text(event.trace_id, 256) for event in events if event.trace_id
        )
    )
    timestamps = [event.timestamp for event in events]
    return QueryLogsOutput(
        status=ToolStatus.SUCCESS,
        duration_ms=_duration_ms(started_at),
        provenance=RuntimeEvidenceProvenance(
            source=result.provenance.source,
            service=tool_input.service,
            environment=tool_input.environment,
            observed_at=result.provenance.observed_at,
            time_range_start=tool_input.time_range_start,
            time_range_end=tool_input.time_range_end,
        ),
        match_count=result.match_count,
        first_seen=min(timestamps) if timestamps else None,
        last_seen=max(timestamps) if timestamps else None,
        error_patterns=[
            ErrorPattern(pattern=pattern, count=count)
            for pattern, count in sorted(
                error_patterns.items(), key=lambda item: (-item[1], item[0])
            )[:MAX_NORMALIZED_LOG_EVENTS]
        ],
        samples=samples[:MAX_NORMALIZED_LOG_EVENTS],
        trace_ids=trace_ids[:MAX_NORMALIZED_LOG_EVENTS],
    )


def _duration_ms(started_at: float) -> float:
    """Return a non-negative elapsed duration for the future ToolCall audit."""
    return max(0.0, round((perf_counter() - started_at) * 1_000, 2))


def _bounded_text(value: str) -> str:
    """Do not let custom adapter records become an unbounded Tool result."""
    return value[:MAX_NORMALIZED_TEXT_CHARS]


def _bounded_optional_text(value: str | None, limit: int) -> str | None:
    return value[:limit] if value else None
