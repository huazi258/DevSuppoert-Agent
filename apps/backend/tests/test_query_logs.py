from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from devsupport_backend.tools.logs import FaultLabLogsAdapter
from devsupport_backend.tools.query_logs import query_logs
from devsupport_backend.tools.schemas import QueryLogsInput, ToolStatus


def test_query_logs_uses_whitelisted_adapter_and_summarizes_structured_events() -> None:
    now = datetime.now(UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.copy_with(query=None) == httpx.URL("http://order-service.test/internal/logs")
        assert request.url.params["level"] == "error"
        assert request.url.params["query"] == "configuration"
        assert request.url.params["limit"] == "10"
        return httpx.Response(
            200,
            json={
                "service": "order-service",
                "match_count": 2,
                "events": [
                    {
                        "timestamp": now.isoformat(),
                        "service": "order-service",
                        "level": "error",
                        "message": "required runtime configuration is missing",
                        "request_id": "request-1",
                        "trace_id": "trace-1",
                        "error_type": "MissingRequiredConfiguration",
                    },
                    {
                        "timestamp": (now + timedelta(seconds=1)).isoformat(),
                        "service": "order-service",
                        "level": "error",
                        "message": "required runtime configuration is missing",
                        "request_id": "request-2",
                        "trace_id": "trace-1",
                        "error_type": "MissingRequiredConfiguration",
                    },
                ],
            },
            request=request,
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = FaultLabLogsAdapter(
        order_service_url="http://order-service.test",
        payment_service_url="http://payment-service.test",
        http_client=http_client,
    )
    output = query_logs(
        QueryLogsInput(
            service="order-service",
            environment="local",
            time_range_start=now - timedelta(minutes=1),
            time_range_end=now + timedelta(minutes=1),
            level="error",
            query="configuration",
            limit=10,
        ),
        adapter,
    )
    http_client.close()

    assert output.status is ToolStatus.SUCCESS
    assert output.match_count == 2
    assert output.first_seen == now
    assert output.last_seen == now + timedelta(seconds=1)
    assert output.error_patterns[0].pattern == "MissingRequiredConfiguration"
    assert output.error_patterns[0].count == 2
    assert len(output.samples) == 2
    assert output.trace_ids == ["trace-1"]
    assert not {"fault_name", "root_cause", "expected_answer", "recommended_action"} & set(
        output.model_dump().keys()
    )


def test_query_logs_rejects_unknown_service_and_returns_empty_results() -> None:
    now = datetime.now(UTC)
    http_client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    adapter = FaultLabLogsAdapter(
        order_service_url="http://order-service.test",
        payment_service_url="http://payment-service.test",
        http_client=http_client,
    )
    failed = query_logs(
        QueryLogsInput(
            service="unknown-service",
            environment="local",
            time_range_start=now - timedelta(minutes=1),
            time_range_end=now,
        ),
        adapter,
    )
    http_client.close()

    assert failed.status is ToolStatus.FAILURE
    assert failed.error is not None
    assert failed.error.code == "unsupported_service"


def test_query_logs_returns_empty_result_without_failure() -> None:
    now = datetime.now(UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"service": "payment-service", "match_count": 0, "events": []},
            request=request,
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = FaultLabLogsAdapter(
        order_service_url="http://order-service.test",
        payment_service_url="http://payment-service.test",
        http_client=http_client,
    )
    output = query_logs(
        QueryLogsInput(
            service="payment-service",
            environment="local",
            time_range_start=now - timedelta(minutes=1),
            time_range_end=now,
        ),
        adapter,
    )
    http_client.close()

    assert output.status is ToolStatus.SUCCESS
    assert output.match_count == 0
    assert output.first_seen is None
    assert output.last_seen is None
    assert output.samples == []
