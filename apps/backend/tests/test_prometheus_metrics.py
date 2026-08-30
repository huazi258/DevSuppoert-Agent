from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from devsupport_backend.config import Settings
from devsupport_backend.tools.adapter_contracts import AdapterError, MetricsAdapter
from devsupport_backend.tools.prometheus_metrics import PrometheusMetricsAdapter
from devsupport_backend.tools.query_metrics import query_metrics
from devsupport_backend.tools.schemas import QueryMetricsInput, ToolStatus


def _input(**overrides: object) -> QueryMetricsInput:
    values: dict[str, object] = {"service": "checkout", "environment": "local"}
    values.update(overrides)
    return QueryMetricsInput(**values)


def _vector(value: object, *, service: str = "checkout") -> dict[str, object]:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": {"service_name": service}, "value": [1_788_087_403.0, value]}],
        },
    }


def _empty_vector() -> dict[str, object]:
    return {"status": "success", "data": {"resultType": "vector", "result": []}}


def _adapter(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[PrometheusMetricsAdapter, httpx.Client]:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return (
        PrometheusMetricsAdapter(prometheus_url="http://prometheus.test", http_client=client),
        client,
    )


def _normal_handler(request: httpx.Request) -> httpx.Response:
    query = request.url.params["query"]
    values = {
        'count by (service_name) (target_info{service_name="checkout"})': "1",
        (
            "sum by (service_name) "
            "(traces_span_metrics_calls_total{service_name=\"checkout\","
            "span_kind=\"SPAN_KIND_SERVER\"})"
        ): "10",
        (
            "sum by (service_name) "
            "(traces_span_metrics_calls_total{service_name=\"checkout\","
            "span_kind=\"SPAN_KIND_SERVER\",status_code=\"STATUS_CODE_ERROR\"})"
        ): "2",
        (
            "sum by (service_name) "
            "(traces_span_metrics_duration_milliseconds_count{service_name=\"checkout\","
            "span_kind=\"SPAN_KIND_SERVER\"})"
        ): "10",
        (
            "sum by (service_name) "
            "(traces_span_metrics_duration_milliseconds_sum{service_name=\"checkout\","
            "span_kind=\"SPAN_KIND_SERVER\"})"
        ): "125.0",
    }
    return httpx.Response(200, json=_vector(values[query]), request=request)


def test_prometheus_adapter_maps_cumulative_server_span_snapshot() -> None:
    adapter, client = _adapter(_normal_handler)

    result = adapter.query(_input())
    client.close()

    assert isinstance(adapter, MetricsAdapter)
    assert result.service == "checkout"
    assert result.health_status == "unknown"
    assert result.request_count == 10
    assert result.error_count == 2
    assert result.success_count == 8
    assert result.error_rate == 0.2
    assert result.average_request_duration_ms == 12.5
    assert result.last_request_duration_ms is None


def test_prometheus_adapter_uses_exact_service_and_server_scope_with_escaped_value() -> None:
    service = 'checkout"} or vector(1) #\nnext'
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        queries.append(query)
        assert 'service_name="checkout\\"} or vector(1) #\\nnext"' in query
        if query.startswith("count by"):
            return httpx.Response(200, json=_vector("1", service=service), request=request)
        assert 'span_kind="SPAN_KIND_SERVER"' in query
        assert "rate(" not in query
        assert "increase(" not in query
        if "status_code" in query:
            value = "0"
        elif "_sum" in query:
            value = "0"
        else:
            value = "0"
        return httpx.Response(200, json=_vector(value, service=service), request=request)

    adapter, client = _adapter(handler)
    result = adapter.query(_input(service=service))
    client.close()

    assert len(queries) == 5
    assert result.request_count == 0
    assert result.error_count == 0
    assert result.average_request_duration_ms is None


def test_prometheus_adapter_treats_non_error_statuses_as_successes() -> None:
    adapter, client = _adapter(_normal_handler)

    result = adapter.query(_input())
    client.close()

    assert result.success_count == result.request_count - result.error_count


def test_prometheus_adapter_allows_zero_request_snapshot_with_presence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        payload = _vector("1") if query.startswith("count by") else _empty_vector()
        return httpx.Response(200, json=payload, request=request)

    adapter, client = _adapter(handler)
    result = adapter.query(_input())
    client.close()

    assert result.request_count == 0
    assert result.success_count == 0
    assert result.error_count == 0
    assert result.average_request_duration_ms is None
    assert result.health_status == "unknown"
    assert result.last_request_duration_ms is None


def test_prometheus_adapter_rejects_service_without_presence() -> None:
    adapter, client = _adapter(
        lambda request: httpx.Response(200, json=_empty_vector(), request=request)
    )

    with pytest.raises(AdapterError) as raised:
        adapter.query(_input(service="unknown-service"))
    client.close()

    assert raised.value.code == "service_metrics_not_found"
    assert not raised.value.retryable


def test_prometheus_adapter_rejects_mismatched_service_labels() -> None:
    adapter, client = _adapter(
        lambda request: httpx.Response(200, json=_vector("1", service="payment"), request=request)
    )

    with pytest.raises(AdapterError) as raised:
        adapter.query(_input())
    client.close()

    assert raised.value.code == "service_mismatch"


def test_prometheus_adapter_rejects_error_count_above_request_count() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        if query.startswith("count by"):
            value = "1"
        elif "status_code" in query:
            value = "2"
        elif "calls_total" in query:
            value = "1"
        elif "_count" in query:
            value = "1"
        else:
            value = "1"
        return httpx.Response(200, json=_vector(value), request=request)

    adapter, client = _adapter(handler)
    with pytest.raises(AdapterError) as raised:
        adapter.query(_input())
    client.close()

    assert raised.value.code == "invalid_prometheus_response"


def test_prometheus_adapter_rejects_missing_duration_sum_when_count_exists() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        if query.startswith("count by"):
            payload = _vector("1")
        elif "_sum" in query:
            payload = _empty_vector()
        elif "status_code" in query:
            payload = _vector("0")
        else:
            payload = _vector("1")
        return httpx.Response(200, json=payload, request=request)

    adapter, client = _adapter(handler)
    with pytest.raises(AdapterError) as raised:
        adapter.query(_input())
    client.close()

    assert raised.value.code == "invalid_prometheus_response"


def test_prometheus_adapter_rejects_multiple_aggregate_snapshots() -> None:
    payload = _vector("1")
    assert isinstance(payload["data"], dict)
    result = payload["data"]["result"]
    assert isinstance(result, list)
    result.append({"metric": {"service_name": "checkout"}, "value": [1_788_087_403.0, "1"]})
    adapter, client = _adapter(lambda request: httpx.Response(200, json=payload, request=request))

    with pytest.raises(AdapterError) as raised:
        adapter.query(_input())
    client.close()

    assert raised.value.code == "invalid_prometheus_response"


@pytest.mark.parametrize("invalid_value", ["NaN", "+Inf", "-Inf", "-1", "1.5"])
def test_prometheus_adapter_rejects_invalid_counter_values(invalid_value: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        value = "1" if query.startswith("count by") else invalid_value
        return httpx.Response(200, json=_vector(value), request=request)

    adapter, client = _adapter(handler)
    with pytest.raises(AdapterError) as raised:
        adapter.query(_input())
    client.close()

    assert raised.value.code == "invalid_prometheus_response"
    assert not raised.value.retryable


@pytest.mark.parametrize(
    ("response", "expected_code", "retryable"),
    [
        (httpx.Response(503), "prometheus_unavailable", True),
        (httpx.Response(429), "prometheus_unavailable", True),
        (httpx.Response(400), "prometheus_query_error", False),
        (
            httpx.Response(200, json={"status": "error", "error": "invalid query"}),
            "prometheus_query_error",
            False,
        ),
        (httpx.Response(200, content=b"not-json"), "invalid_prometheus_response", False),
    ],
)
def test_prometheus_adapter_maps_provider_errors_to_safe_boundary(
    response: httpx.Response, expected_code: str, retryable: bool
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers=response.headers,
            request=request,
        )

    adapter, client = _adapter(handler)
    with pytest.raises(AdapterError) as raised:
        adapter.query(_input())
    client.close()

    assert raised.value.code == expected_code
    assert raised.value.retryable is retryable


@pytest.mark.parametrize("error", [httpx.ConnectError("offline"), httpx.ReadTimeout("stalled")])
def test_prometheus_adapter_maps_transport_errors_as_retryable(error: Exception) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise type(error)(str(error), request=request)

    adapter, client = _adapter(handler)
    with pytest.raises(AdapterError) as raised:
        adapter.query(_input())
    client.close()

    assert raised.value.code == "prometheus_unavailable"
    assert raised.value.retryable


def test_prometheus_adapter_rejects_unsupported_environment_without_http_request() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        raise AssertionError("unsupported environment must not send an HTTP request")

    adapter, client = _adapter(handler)
    with pytest.raises(AdapterError) as raised:
        adapter.query(_input(environment="staging"))
    client.close()

    assert raised.value.code == "unsupported_environment"
    assert not called


def test_prometheus_adapter_remains_compatible_with_query_metrics_tool() -> None:
    adapter, client = _adapter(_normal_handler)

    output = query_metrics(_input(), adapter)
    client.close()

    assert output.status is ToolStatus.SUCCESS
    assert output.metrics is not None
    assert output.metrics.model_dump() == {
        "service": "checkout",
        "environment": "local",
        "health_status": "unknown",
        "request_count": 10,
        "success_count": 8,
        "error_count": 2,
        "error_rate": 0.2,
        "last_request_duration_ms": None,
        "average_request_duration_ms": 12.5,
    }


def test_prometheus_url_supports_both_environment_variable_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)
    monkeypatch.delenv("DEVSUPPORT_PROMETHEUS_URL", raising=False)
    assert Settings().prometheus_url is None

    monkeypatch.setenv("PROMETHEUS_URL", "http://prometheus.test")
    assert Settings().prometheus_url == "http://prometheus.test"
    monkeypatch.delenv("PROMETHEUS_URL")
    monkeypatch.setenv("DEVSUPPORT_PROMETHEUS_URL", "http://devsupport-prometheus.test")
    assert Settings().prometheus_url == "http://devsupport-prometheus.test"


def test_prometheus_adapter_requires_explicit_endpoint_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)
    monkeypatch.delenv("DEVSUPPORT_PROMETHEUS_URL", raising=False)
    with pytest.raises(AdapterError) as raised:
        PrometheusMetricsAdapter.from_settings(Settings())

    assert raised.value.code == "missing_prometheus_configuration"
