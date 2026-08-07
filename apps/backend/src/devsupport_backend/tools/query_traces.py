"""Structured Tool executor for bounded Fault Lab distributed trace evidence."""

from collections import defaultdict
from time import perf_counter

from devsupport_backend.tools.schemas import (
    QueryTracesInput,
    QueryTracesOutput,
    ToolError,
    ToolStatus,
    TraceError,
    TraceSpan,
    TraceSummary,
)
from devsupport_backend.tools.traces import (
    FaultLabTracesAdapter,
    FaultLabTraceSpan,
    TracesAdapterError,
)


def query_traces(
    tool_input: QueryTracesInput,
    traces_adapter: FaultLabTracesAdapter,
) -> QueryTracesOutput:
    """Collect fixed-service span buffers and return reconstructed trace evidence."""
    started_at = perf_counter()
    try:
        result = traces_adapter.query(tool_input)
    except TracesAdapterError as error:
        return QueryTracesOutput(
            status=ToolStatus.FAILURE,
            error=ToolError(code=error.code, message=str(error), retryable=error.retryable),
            duration_ms=_duration_ms(started_at),
        )

    return QueryTracesOutput(
        status=ToolStatus.SUCCESS,
        duration_ms=_duration_ms(started_at),
        traces=_summarize_traces(
            result.spans,
            anchor_service=tool_input.service,
            limit=tool_input.limit,
        ),
    )


def _summarize_traces(
    records: list[FaultLabTraceSpan],
    *,
    anchor_service: str,
    limit: int,
) -> list[TraceSummary]:
    """Merge service-local spans by trace ID while retaining parent/child facts."""
    grouped: dict[str, dict[str, FaultLabTraceSpan]] = defaultdict(dict)
    for record in records:
        grouped[record.trace_id][record.span_id] = record

    summaries: list[tuple[float, TraceSummary]] = []
    for trace_id, unique_spans in grouped.items():
        records_for_trace = list(unique_spans.values())
        if not any(record.service == anchor_service for record in records_for_trace):
            continue
        spans = sorted(
            (_to_trace_span(record) for record in records_for_trace),
            key=lambda span: (span.start_time, span.span_id),
        )
        errors = [
            TraceError(
                service=span.service,
                span_id=span.span_id,
                operation=span.operation,
                message=span.error,
            )
            for span in spans
            if span.error is not None
        ]
        start_time = min(span.start_time for span in spans)
        end_time = max(span.end_time for span in spans)
        slowest_span = max(spans, key=lambda span: (span.duration_ms, span.span_id))
        summary = TraceSummary(
            trace_id=trace_id,
            duration_ms=round((end_time - start_time).total_seconds() * 1_000, 2),
            status="error" if errors else "ok",
            spans=spans,
            errors=errors,
            slowest_span=slowest_span,
        )
        summaries.append((end_time.timestamp(), summary))
    newest_first = sorted(summaries, key=lambda item: item[0], reverse=True)
    return [summary for _, summary in newest_first[:limit]]


def _to_trace_span(record: FaultLabTraceSpan) -> TraceSpan:
    """Convert the adapter contract without dropping timing or relationship facts."""
    return TraceSpan(
        span_id=record.span_id,
        parent_span_id=record.parent_span_id,
        service=record.service,
        operation=record.operation,
        start_time=record.start_time,
        end_time=record.end_time,
        duration_ms=record.duration_ms,
        status=record.status,
        error=record.error,
    )


def _duration_ms(started_at: float) -> float:
    """Return a non-negative elapsed duration for the future ToolCall audit."""
    return max(0.0, round((perf_counter() - started_at) * 1_000, 2))
