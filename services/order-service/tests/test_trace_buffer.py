from __future__ import annotations

from datetime import UTC, datetime, timedelta

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

from order_service.trace_buffer import SpanBuffer


def test_span_buffer_is_bounded_and_preserves_real_parent_relationships() -> None:
    provider = TracerProvider(resource=Resource.create({"service.name": "order-service"}))
    buffer = SpanBuffer(capacity=2)
    provider.add_span_processor(buffer)
    tracer = provider.get_tracer("test")
    started_at = datetime.now(UTC)

    with tracer.start_as_current_span("parent") as parent:
        parent_span_id = f"{parent.get_span_context().span_id:016x}"
        with tracer.start_as_current_span("child") as child:
            trace_id = f"{child.get_span_context().trace_id:032x}"

    match_count, spans = buffer.query(
        time_range_start=started_at - timedelta(seconds=1),
        time_range_end=datetime.now(UTC) + timedelta(seconds=1),
        trace_id=trace_id,
        limit=20,
    )
    provider.shutdown()

    assert match_count == 2
    assert [span["operation"] for span in spans] == ["child", "parent"]
    assert spans[0]["parent_span_id"] == parent_span_id
    assert all(span["trace_id"] == trace_id for span in spans)
    assert all(span["service"] == "order-service" for span in spans)
