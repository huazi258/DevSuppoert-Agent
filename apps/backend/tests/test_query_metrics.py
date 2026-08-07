from __future__ import annotations

import httpx
import pytest

from devsupport_backend.tools.metrics import FaultLabMetricsAdapter
from devsupport_backend.tools.query_metrics import query_metrics
from devsupport_backend.tools.schemas import QueryMetricsInput, ToolStatus


def _adapter(handler: httpx.MockTransport) -> FaultLabMetricsAdapter:
    return FaultLabMetricsAdapter(
        order_service_url="http://order-service.test",
        payment_service_url="http://payment-service.test",
        http_client=httpx.Client(transport=handler, timeout=1.0),
    )


def test_query_metrics_combines_fixed_metrics_and_health_endpoints() -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "order-service.test"
        requested_paths.append(request.url.path)
        if request.url.path == "/internal/metrics":
            return httpx.Response(
                200,
                json={
                    "service": "order-service",
                    "request_count": 10,
                    "success_count": 8,
                    "error_count": 2,
                    "last_request_duration_ms": 13.5,
                },
            )
        if request.url.path == "/health":
            return httpx.Response(200, json={"service": "order-service", "status": "ok"})
        raise AssertionError(f"unexpected URL: {request.url}")

    output = query_metrics(
        QueryMetricsInput(service="order-service", environment="local"),
        _adapter(httpx.MockTransport(handler)),
    )

    assert requested_paths == ["/internal/metrics", "/health"]
    assert output.status is ToolStatus.SUCCESS
    assert output.error is None
    assert output.metrics is not None
    assert output.metrics.model_dump() == {
        "service": "order-service",
        "environment": "local",
        "health_status": "ok",
        "request_count": 10,
        "success_count": 8,
        "error_count": 2,
        "error_rate": 0.2,
        "last_request_duration_ms": 13.5,
        "average_request_duration_ms": None,
    }


@pytest.mark.parametrize(
    ("tool_input", "error_code"),
    [
        (QueryMetricsInput(service="unknown-service", environment="local"), "unsupported_service"),
        (
            QueryMetricsInput(service="order-service", environment="staging"),
            "unsupported_environment",
        ),
    ],
)
def test_query_metrics_rejects_non_whitelisted_fault_lab_targets(
    tool_input: QueryMetricsInput,
    error_code: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"adapter should not request {request.url}")

    output = query_metrics(tool_input, _adapter(httpx.MockTransport(handler)))

    assert output.status is ToolStatus.FAILURE
    assert output.error is not None
    assert output.error.code == error_code
    assert output.metrics is None


def test_query_metrics_returns_structured_failure_when_fault_lab_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    output = query_metrics(
        QueryMetricsInput(service="payment-service", environment="local"),
        _adapter(httpx.MockTransport(handler)),
    )

    assert output.status is ToolStatus.FAILURE
    assert output.error is not None
    assert output.error.code == "fault_lab_unavailable"
    assert output.error.retryable
    assert output.metrics is None
