"""Bounded storage for real, ended OpenTelemetry spans from this service."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.trace.status import StatusCode

MAX_TRACE_SPANS = 500


class SpanBuffer(SpanProcessor):
    """Keep a bounded, thread-safe projection of finished service-local spans."""

    def __init__(self, capacity: int = MAX_TRACE_SPANS) -> None:
        self._spans: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = Lock()

    def on_start(self, span: object, parent_context: object | None = None) -> None:
        """Span start is intentionally not retained; only completed evidence is queryable."""

    def on_end(self, span: ReadableSpan) -> None:
        """Store a compact projection of the exact OpenTelemetry span that ended."""
        if span.start_time is None or span.end_time is None:
            return
        context = span.get_span_context()
        if not context.is_valid:
            return
        parent = span.parent
        start_time = datetime.fromtimestamp(span.start_time / 1_000_000_000, UTC)
        end_time = datetime.fromtimestamp(span.end_time / 1_000_000_000, UTC)
        record = {
            "trace_id": f"{context.trace_id:032x}",
            "span_id": f"{context.span_id:016x}",
            "parent_span_id": f"{parent.span_id:016x}" if parent and parent.is_valid else None,
            "service": str(span.resource.attributes.get("service.name", "unknown")),
            "operation": span.name,
            "start_time": start_time,
            "end_time": end_time,
            "duration_ms": round((span.end_time - span.start_time) / 1_000_000, 2),
            "status": "error" if span.status.status_code is StatusCode.ERROR else "ok",
            "error": _error_message(span),
        }
        with self._lock:
            self._spans.append(record)

    def shutdown(self) -> None:
        """The in-memory processor has no external resources to close."""

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """Ended spans are synchronously available without a flush operation."""
        return True

    def clear(self) -> None:
        """Clear local span history for isolated Fault Lab tests."""
        with self._lock:
            self._spans.clear()

    def query(
        self,
        *,
        time_range_start: datetime,
        time_range_end: datetime,
        trace_id: str | None,
        limit: int,
    ) -> tuple[int, list[dict[str, Any]]]:
        """Return newest matching spans with the count before applying the limit."""
        with self._lock:
            matching_spans = [
                dict(span)
                for span in self._spans
                if time_range_start <= span["end_time"] <= time_range_end
                and (trace_id is None or span["trace_id"] == trace_id)
            ]
        return len(matching_spans), matching_spans[-limit:]


def _error_message(span: ReadableSpan) -> str | None:
    """Keep a concise error fact without retaining exception stack traces."""
    if span.status.status_code is not StatusCode.ERROR:
        return None
    if span.status.description:
        return span.status.description
    for event in span.events:
        if event.name != "exception":
            continue
        error_type = event.attributes.get("exception.type")
        error_message = event.attributes.get("exception.message")
        return ": ".join(str(value) for value in (error_type, error_message) if value)
    return "span completed with an error"
