from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from devsupport_backend.config import Settings
from devsupport_backend.tools.adapter_contracts import AdapterError, LogsAdapter
from devsupport_backend.tools.opensearch_logs import OpenSearchLogsAdapter
from devsupport_backend.tools.query_logs import query_logs
from devsupport_backend.tools.schemas import QueryLogsInput, ToolStatus

NOW = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)


def _input(**overrides: object) -> QueryLogsInput:
    values: dict[str, object] = {
        "service": "checkout",
        "environment": "local",
        "time_range_start": NOW - timedelta(minutes=5),
        "time_range_end": NOW,
        "limit": 2,
    }
    values.update(overrides)
    return QueryLogsInput(**values)


def _response(
    *,
    service: str = "checkout",
    body: object = "payment timed out",
    severity: str = "ERROR",
) -> dict[str, object]:
    return {
        "hits": {
            "total": {"value": 3, "relation": "eq"},
            "hits": [
                {
                    "_index": "otel-logs-ignored",
                    "_score": 1.0,
                    "_source": {
                        "@timestamp": (NOW + timedelta(seconds=1)).isoformat(),
                        "body": body,
                        "severity": {"text": severity, "number": 17},
                        "resource": {"service.name": service, "service.version": "ignored"},
                        "traceId": "trace-2",
                        "attributes": {"exception.type": "ReadTimeout", "unused": "ignored"},
                    },
                },
                {
                    "_source": {
                        "@timestamp": NOW.isoformat(),
                        "body": "payment connection failed",
                        "severity": {"text": "error"},
                        "resource": {"service.name": service},
                        "traceId": "trace-1",
                    }
                },
            ],
        }
    }


def _adapter(handler: httpx.MockTransport) -> tuple[OpenSearchLogsAdapter, httpx.Client]:
    client = httpx.Client(transport=handler)
    return (
        OpenSearchLogsAdapter(opensearch_url="http://opensearch.test", http_client=client),
        client,
    )


def test_opensearch_adapter_uses_bounded_structured_search_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("http://opensearch.test/otel-logs-*/_search")
        body = json.loads(request.content)
        assert body["size"] == 2
        assert body["track_total_hits"] is True
        assert body["sort"] == [{"@timestamp": {"order": "desc"}}]
        assert body["query"] == {
            "bool": {
                "filter": [
                    {"term": {"resource.service.name": "checkout"}},
                    {
                        "range": {
                            "@timestamp": {
                                "gte": (NOW - timedelta(minutes=5)).isoformat(),
                                "lte": NOW.isoformat(),
                            }
                        }
                    },
                    {"terms": {"severity.text.keyword": ["ERROR", "error"]}},
                    {"match": {"body": {"query": "payment timeout"}}},
                ]
            }
        }
        return httpx.Response(200, json=_response(), request=request)

    adapter, client = _adapter(httpx.MockTransport(handler))
    result = adapter.query(_input(level="error", query="payment timeout"))
    client.close()

    assert result.match_count == 3
    assert [event.timestamp for event in result.events] == [NOW, NOW + timedelta(seconds=1)]


def test_opensearch_adapter_normalizes_real_payload_shape_and_total_hits() -> None:
    adapter, client = _adapter(
        httpx.MockTransport(lambda request: httpx.Response(200, json=_response()))
    )

    result = adapter.query(_input())
    client.close()

    assert isinstance(adapter, LogsAdapter)
    assert result.match_count == 3
    assert result.events[1].level == "error"
    assert result.events[1].trace_id == "trace-2"
    assert result.events[1].error_type == "ReadTimeout"
    assert result.events[1].request_id is None
    assert result.events[1].status_code is None
    assert result.events[1].duration_ms is None
    assert result.events[1].downstream_service is None
    assert result.events[0].error_type is None


def test_opensearch_adapter_maps_unknown_severity_to_safe_non_error_value() -> None:
    adapter, client = _adapter(
        httpx.MockTransport(
            lambda request: httpx.Response(200, json=_response(severity="NOTICE"))
        )
    )

    result = adapter.query(_input())
    client.close()

    assert result.events[1].level == "unknown"


def test_opensearch_adapter_accepts_empty_hits_and_integer_total() -> None:
    response = {"hits": {"total": 0, "hits": []}}
    adapter, client = _adapter(
        httpx.MockTransport(lambda request: httpx.Response(200, json=response))
    )

    result = adapter.query(_input())
    client.close()

    assert result.match_count == 0
    assert result.events == ()


def test_opensearch_adapter_rejects_service_mismatch() -> None:
    adapter, client = _adapter(
        httpx.MockTransport(lambda request: httpx.Response(200, json=_response(service="payment")))
    )

    with pytest.raises(AdapterError, match="different service") as raised:
        adapter.query(_input())
    client.close()

    assert raised.value.code == "service_mismatch"
    assert not raised.value.retryable


def test_opensearch_adapter_rejects_unsupported_environment_without_http_request() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        raise AssertionError("unsupported environment must not send an HTTP request")

    adapter, client = _adapter(httpx.MockTransport(handler))
    with pytest.raises(AdapterError) as raised:
        adapter.query(_input(environment="staging"))
    client.close()

    assert raised.value.code == "unsupported_environment"
    assert not called


@pytest.mark.parametrize(
    ("response", "expected_code", "retryable"),
    [
        (httpx.Response(503), "opensearch_unavailable", True),
        (httpx.Response(429), "opensearch_unavailable", True),
        (httpx.Response(400), "opensearch_query_error", False),
        (httpx.Response(200, content=b"not-json"), "invalid_opensearch_response", False),
    ],
)
def test_opensearch_adapter_maps_provider_errors_to_safe_boundary(
    response: httpx.Response, expected_code: str, retryable: bool
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers=response.headers,
            request=request,
        )

    adapter, client = _adapter(httpx.MockTransport(handler))
    with pytest.raises(AdapterError) as raised:
        adapter.query(_input())
    client.close()

    assert raised.value.code == expected_code
    assert raised.value.retryable is retryable


def test_opensearch_adapter_maps_transport_error_as_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    adapter, client = _adapter(httpx.MockTransport(handler))
    with pytest.raises(AdapterError) as raised:
        adapter.query(_input())
    client.close()

    assert raised.value.code == "opensearch_unavailable"
    assert raised.value.retryable


def test_opensearch_adapter_maps_timeout_as_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    adapter, client = _adapter(httpx.MockTransport(handler))
    with pytest.raises(AdapterError) as raised:
        adapter.query(_input())
    client.close()

    assert raised.value.code == "opensearch_unavailable"
    assert raised.value.retryable


def test_opensearch_adapter_rejects_malformed_or_non_scalar_log_events() -> None:
    adapter, client = _adapter(
        httpx.MockTransport(
            lambda request: httpx.Response(200, json=_response(body={"raw": "payload"}))
        )
    )
    with pytest.raises(AdapterError) as raised:
        adapter.query(_input())
    client.close()

    assert raised.value.code == "invalid_opensearch_response"


def test_opensearch_adapter_bounds_message_without_leaking_provider_fields() -> None:
    adapter, client = _adapter(
        httpx.MockTransport(lambda request: httpx.Response(200, json=_response(body="x" * 2_001)))
    )

    result = adapter.query(_input())
    client.close()

    assert len(result.events[1].message) == 2_000


def test_opensearch_adapter_remains_compatible_with_query_logs_tool() -> None:
    adapter, client = _adapter(
        httpx.MockTransport(lambda request: httpx.Response(200, json=_response()))
    )

    output = query_logs(_input(), adapter)
    client.close()

    assert output.status is ToolStatus.SUCCESS
    assert output.match_count == 3
    assert output.first_seen == NOW
    assert output.last_seen == NOW + timedelta(seconds=1)
    assert output.error_patterns[0].model_dump() == {"pattern": "ReadTimeout", "count": 1}
    assert output.error_patterns[1].model_dump() == {
        "pattern": "payment connection failed",
        "count": 1,
    }
    assert output.trace_ids == ["trace-1", "trace-2"]


def test_opensearch_url_supports_both_environment_variable_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENSEARCH_URL", raising=False)
    monkeypatch.delenv("DEVSUPPORT_OPENSEARCH_URL", raising=False)
    assert Settings().opensearch_url is None

    monkeypatch.setenv("OPENSEARCH_URL", "http://opensearch.test")
    assert Settings().opensearch_url == "http://opensearch.test"
    monkeypatch.delenv("OPENSEARCH_URL")
    monkeypatch.setenv("DEVSUPPORT_OPENSEARCH_URL", "http://devsupport-opensearch.test")
    assert Settings().opensearch_url == "http://devsupport-opensearch.test"


def test_opensearch_adapter_requires_explicit_endpoint_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENSEARCH_URL", raising=False)
    monkeypatch.delenv("DEVSUPPORT_OPENSEARCH_URL", raising=False)
    with pytest.raises(AdapterError) as raised:
        OpenSearchLogsAdapter.from_settings(Settings())

    assert raised.value.code == "missing_opensearch_configuration"
