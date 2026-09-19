"""Structured Tool executor for bounded distributed trace evidence."""

from collections import defaultdict
from collections.abc import Sequence
from time import perf_counter

from devsupport_backend.tools.adapter_contracts import (
    MAX_NORMALIZED_TRACE_SPANS,
    AdapterError,
    TracesAdapter,
    TraceSpanRecord,
    normalize_adapter_error,
)
from devsupport_backend.tools.schemas import (
    QueryTracesInput,
    QueryTracesOutput,
    RuntimeEvidenceProvenance,
    ToolError,
    ToolStatus,
    TraceError,
    TraceSpan,
    TraceSummary,
)


def query_traces(
    tool_input: QueryTracesInput,
    traces_adapter: TracesAdapter,
) -> QueryTracesOutput:
    """Collect fixed-service span buffers and return reconstructed trace evidence."""
    started_at = perf_counter()
    try:
        result = traces_adapter.query(tool_input)
    except AdapterError as error:
        normalized_error = normalize_adapter_error(error)
        return QueryTracesOutput(
            status=ToolStatus.FAILURE,
            error=ToolError(**normalized_error.__dict__),
            duration_ms=_duration_ms(started_at),
        )

    return QueryTracesOutput(
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
        traces=_summarize_traces(
            result.spans,
            anchor_service=tool_input.service,
            limit=tool_input.limit,
        ),
    )


def _summarize_traces(
    records: Sequence[TraceSpanRecord],
    *,
    anchor_service: str,
    limit: int,
) -> list[TraceSummary]:
    """Merge service-local spans by trace ID while retaining parent/child facts."""
    grouped: dict[str, dict[str, TraceSpanRecord]] = defaultdict(dict)
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
        )[:MAX_NORMALIZED_TRACE_SPANS]
        errors = [
            TraceError(
                service=span.service,
                span_id=span.span_id,
                operation=span.operation,
                message=span.error,
            )
            for span in spans
            if span.error is not None
        ][:MAX_NORMALIZED_TRACE_SPANS]
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


def _to_trace_span(record: TraceSpanRecord) -> TraceSpan:
    """Convert the adapter contract without dropping timing or relationship facts."""
    return TraceSpan(
        span_id=record.span_id[:256],
        parent_span_id=record.parent_span_id[:256] if record.parent_span_id else None,
        service=record.service[:100],
        operation=record.operation[:500],
        start_time=record.start_time,
        end_time=record.end_time,
        duration_ms=record.duration_ms,
        status=record.status[:50],
        error=record.error[:1_000] if record.error else None,
    )


def _duration_ms(started_at: float) -> float:
    """Return a non-negative elapsed duration for the future ToolCall audit."""
    return max(0.0, round((perf_counter() - started_at) * 1_000, 2))
