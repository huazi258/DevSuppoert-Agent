from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from devsupport_backend.tools.adapter_contracts import TraceSpanRecord
from devsupport_backend.tools.query_traces import _summarize_traces, query_traces
from devsupport_backend.tools.schemas import QueryTracesInput, ToolStatus
from devsupport_backend.tools.traces import FaultLabTracesAdapter


def _adapter(handler: httpx.MockTransport) -> FaultLabTracesAdapter:
    return FaultLabTracesAdapter(
        order_service_url="http://order-service.test",
        payment_service_url="http://payment-service.test",
        http_client=httpx.Client(transport=handler, timeout=1.0),
    )


def _span(
    *,
    trace_id: str,
    span_id: str,
    parent_span_id: str | None,
    service: str,
    operation: str,
    start_time: datetime,
    duration_ms: float,
    status: str = "ok",
    error: str | None = None,
) -> dict[str, object]:
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "service": service,
        "operation": operation,
        "start_time": start_time.isoformat(),
        "end_time": (start_time + timedelta(milliseconds=duration_ms)).isoformat(),
        "duration_ms": duration_ms,
        "status": status,
        "error": error,
    }


def _contract_record(payload: dict[str, object]) -> TraceSpanRecord:
    return TraceSpanRecord(
        trace_id=str(payload["trace_id"]),
        span_id=str(payload["span_id"]),
        parent_span_id=(
            payload["parent_span_id"]
            if isinstance(payload["parent_span_id"], str)
            else None
        ),
        service=str(payload["service"]),
        operation=str(payload["operation"]),
        start_time=datetime.fromisoformat(str(payload["start_time"])),
        end_time=datetime.fromisoformat(str(payload["end_time"])),
        duration_ms=float(payload["duration_ms"]),
        status=str(payload["status"]),
        error=payload["error"] if isinstance(payload["error"], str) else None,
    )


def test_query_traces_aggregates_cross_service_spans_and_identifies_slowest_span() -> None:
    now = datetime.now(UTC)
    requested_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(request.url.host)
        assert request.url.path == "/internal/traces"
        assert request.url.params["limit"] == "20"
        assert request.url.params["trace_id"] == "trace-timeout"
        if request.url.host == "order-service.test":
            return httpx.Response(
                200,
                json={
                    "service": "order-service",
                    "match_count": 2,
                    "spans": [
                        _span(
                            trace_id="trace-timeout",
                            span_id="order-root",
                            parent_span_id=None,
                            service="order-service",
                            operation="POST /orders",
                            start_time=now,
                            duration_ms=3_000,
                            status="error",
                            error="Internal Server Error",
                        ),
                        _span(
                            trace_id="trace-timeout",
                            span_id="order-client",
                            parent_span_id="order-root",
                            service="order-service",
                            operation="POST",
                            start_time=now + timedelta(milliseconds=100),
                            duration_ms=2_900,
                            status="error",
                            error="ReadTimeout",
                        ),
                    ],
                },
            )
        if request.url.host == "payment-service.test":
            return httpx.Response(
                200,
                json={
                    "service": "payment-service",
                    "match_count": 1,
                    "spans": [
                        _span(
                            trace_id="trace-timeout",
                            span_id="payment-server",
                            parent_span_id="order-client",
                            service="payment-service",
                            operation="POST /payments",
                            start_time=now + timedelta(milliseconds=200),
                            duration_ms=4_000,
                        )
                    ],
                },
            )
        raise AssertionError(f"unexpected URL: {request.url}")

    output = query_traces(
        QueryTracesInput(
            service="order-service",
            environment="local",
            time_range_start=now - timedelta(minutes=1),
            time_range_end=now + timedelta(minutes=1),
            trace_id="trace-timeout",
        ),
        _adapter(httpx.MockTransport(handler)),
    )

    assert requested_hosts == ["order-service.test", "payment-service.test"]
    assert output.status is ToolStatus.SUCCESS
    assert len(output.traces) == 1
    trace = output.traces[0]
    assert trace.trace_id == "trace-timeout"
    assert trace.status == "error"
    assert [span.span_id for span in trace.spans] == [
        "order-root",
        "order-client",
        "payment-server",
    ]
    assert trace.spans[1].parent_span_id == "order-root"
    assert trace.spans[2].parent_span_id == "order-client"
    assert trace.slowest_span is not None
    assert trace.slowest_span.service == "payment-service"
    assert trace.slowest_span.duration_ms == 4_000
    assert [error.message for error in trace.errors] == ["Internal Server Error", "ReadTimeout"]
    assert not {"fault_name", "root_cause", "expected_answer", "recommended_action"} & set(
        output.model_dump()
    )


def test_trace_summary_filters_by_anchor_service_and_applies_limit() -> None:
    now = datetime.now(UTC)
    records = [
        _contract_record(
            _span(
                trace_id="trace-order",
                span_id="order-span",
                parent_span_id=None,
                service="order-service",
                operation="POST /orders",
                start_time=now,
                duration_ms=10,
            )
        ),
        _contract_record(
            _span(
                trace_id="trace-payment",
                span_id="payment-span",
                parent_span_id=None,
                service="payment-service",
                operation="POST /payments",
                start_time=now + timedelta(seconds=1),
                duration_ms=10,
            )
        ),
        _contract_record(
            _span(
                trace_id="trace-order-later",
                span_id="order-later-span",
                parent_span_id=None,
                service="order-service",
                operation="POST /orders",
                start_time=now + timedelta(seconds=2),
                duration_ms=10,
            )
        ),
    ]

    summaries = _summarize_traces(records, anchor_service="order-service", limit=1)

    assert [summary.trace_id for summary in summaries] == ["trace-order-later"]


@pytest.mark.parametrize(
    ("tool_input", "error_code"),
    [
        (
            QueryTracesInput(
                service="unknown-service",
                environment="local",
                time_range_start=datetime.now(UTC),
                time_range_end=datetime.now(UTC),
            ),
            "unsupported_service",
        ),
        (
            QueryTracesInput(
                service="order-service",
                environment="staging",
                time_range_start=datetime.now(UTC),
                time_range_end=datetime.now(UTC),
            ),
            "unsupported_environment",
        ),
    ],
)
def test_query_traces_rejects_non_whitelisted_fault_lab_targets(
    tool_input: QueryTracesInput,
    error_code: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"adapter should not request {request.url}")

    output = query_traces(tool_input, _adapter(httpx.MockTransport(handler)))

    assert output.status is ToolStatus.FAILURE
    assert output.error is not None
    assert output.error.code == error_code
    assert output.traces == []


def test_query_traces_returns_structured_failure_when_fault_lab_is_unavailable() -> None:
    now = datetime.now(UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    output = query_traces(
        QueryTracesInput(
            service="order-service",
            environment="local",
            time_range_start=now - timedelta(minutes=1),
            time_range_end=now,
            trace_id="trace-id",
        ),
        _adapter(httpx.MockTransport(handler)),
    )

    assert output.status is ToolStatus.FAILURE
    assert output.error is not None
    assert output.error.code == "fault_lab_unavailable"
    assert output.error.retryable
