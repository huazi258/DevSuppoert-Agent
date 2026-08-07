import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

from order_service.fault_control import inject_missing_config_fault, reset_fault_lab
from order_service.main import REQUEST_ID_HEADER, app, get_payment_client, log_buffer, telemetry

client = TestClient(app)


@pytest.fixture(autouse=True)
def clear_dependency_overrides() -> None:
    async def default_payment_client():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(500, request=request)),
            base_url="http://payment-service.test",
        ) as payment_client:
            yield payment_client

    reset_fault_lab()
    log_buffer.clear()
    telemetry.span_buffer.clear()
    app.dependency_overrides.clear()
    app.dependency_overrides[get_payment_client] = default_payment_client
    yield
    app.dependency_overrides.clear()
    log_buffer.clear()
    telemetry.span_buffer.clear()
    reset_fault_lab()


def mock_payment_service(handler: Callable[[httpx.Request], httpx.Response]) -> None:
    async def get_mock_client():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://payment-service.test"
        ) as payment_client:
            HTTPXClientInstrumentor.instrument_client(payment_client)
            yield payment_client

    app.dependency_overrides[get_payment_client] = get_mock_client


def test_health_check_returns_service_identity() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "order-service"}


def test_internal_traces_returns_real_ended_spans_with_filters() -> None:
    started_at = datetime.now(UTC)
    health_response = client.get("/health")
    traces_response = client.get(
        "/internal/traces",
        params={
            "time_range_start": (started_at - timedelta(seconds=1)).isoformat(),
            "time_range_end": (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
            "limit": 20,
        },
    )

    assert health_response.status_code == 200
    assert traces_response.status_code == 200
    traces = traces_response.json()
    assert traces["service"] == "order-service"
    health_span = next(span for span in traces["spans"] if span["operation"] == "GET /health")
    filtered_response = client.get(
        "/internal/traces",
        params={
            "time_range_start": (started_at - timedelta(seconds=1)).isoformat(),
            "time_range_end": (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
            "trace_id": health_span["trace_id"],
            "limit": 1,
        },
    )

    filtered = filtered_response.json()
    assert filtered_response.status_code == 200
    assert filtered["match_count"] >= 1
    assert len(filtered["spans"]) == 1
    assert filtered["spans"][0]["trace_id"] == health_span["trace_id"]
    assert health_span["span_id"]
    assert health_span["start_time"]
    assert health_span["end_time"]
    assert health_span["status"] == "ok"
    assert not {"fault_name", "root_cause", "expected_answer", "recommended_action"} & set(
        health_span
    )


def test_deployment_reports_clean_order_service_baseline() -> None:
    response = client.get("/internal/deployment")

    assert response.status_code == 200
    assert response.json() == {
        "service": "order-service",
        "current_version": "v1.0.0",
        "previous_version": None,
        "deployed_at": None,
    }


def test_lifespan_makes_a_shared_payment_client_available() -> None:
    with TestClient(app) as lifespan_client:
        payment_client = lifespan_client.app.state.payment_client

        assert isinstance(payment_client, httpx.AsyncClient)
        assert not payment_client.is_closed

    assert payment_client.is_closed


def test_create_order_confirms_after_payment_approval() -> None:
    def payment_handler(request: httpx.Request) -> httpx.Response:
        payment_request = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "payment_id": "pay-123",
                "order_id": payment_request["order_id"],
                "status": "approved",
            },
        )

    mock_payment_service(payment_handler)

    response = client.post("/orders", json={"amount": 99.9})

    assert response.status_code == 200
    assert response.json()["order_id"].startswith("order-")
    assert response.json()["payment_id"] == "pay-123"
    assert response.json()["status"] == "confirmed"


def test_payment_request_contains_generated_order_id_and_amount() -> None:
    captured_request: dict[str, object] = {}

    def payment_handler(request: httpx.Request) -> httpx.Response:
        captured_request.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "payment_id": "pay-123",
                "order_id": captured_request["order_id"],
                "status": "approved",
            },
        )

    mock_payment_service(payment_handler)

    response = client.post("/orders", json={"amount": 18.5})

    assert response.status_code == 200
    assert str(captured_request["order_id"]).startswith("order-")
    assert captured_request["amount"] == 18.5


def test_request_id_is_propagated_to_payment_service_and_response() -> None:
    request_id = "request-test-123"
    captured_headers: dict[str, str] = {}

    def payment_handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(request.headers)
        order_id = json.loads(request.content)["order_id"]
        return httpx.Response(
            200,
            json={"payment_id": "pay-123", "order_id": order_id, "status": "approved"},
        )

    mock_payment_service(payment_handler)

    response = client.post(
        "/orders",
        json={"amount": 99.9},
        headers={REQUEST_ID_HEADER: request_id},
    )

    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER] == request_id
    assert captured_headers[REQUEST_ID_HEADER.lower()] == request_id
    assert "traceparent" in captured_headers


@pytest.mark.parametrize("amount", [0, -1])
def test_create_order_rejects_non_positive_amount(amount: int) -> None:
    response = client.post("/orders", json={"amount": amount})

    assert response.status_code == 422


def test_create_order_returns_bad_gateway_when_payment_service_is_unavailable() -> None:
    def payment_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    mock_payment_service(payment_handler)

    response = client.post("/orders", json={"amount": 99.9})

    assert response.status_code == 502
    assert response.json()["detail"] == "Payment service unavailable"


def test_create_order_returns_bad_gateway_when_payment_service_rejects_request() -> None:
    def payment_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "unavailable"}, request=request)

    mock_payment_service(payment_handler)

    response = client.post("/orders", json={"amount": 99.9})

    assert response.status_code == 502
    assert response.json()["detail"] == "Payment service unavailable"


def test_create_order_returns_bad_gateway_when_payment_service_times_out() -> None:
    def payment_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("payment-service timed out", request=request)

    mock_payment_service(payment_handler)

    response = client.post("/orders", json={"amount": 99.9})

    assert response.status_code == 502
    assert response.json()["detail"] == "Payment service unavailable"


def test_missing_configuration_returns_internal_server_error() -> None:
    inject_missing_config_fault()

    response = client.post("/orders", json={"amount": 99.9})
    deployment_response = client.get("/internal/deployment")

    assert response.status_code == 500
    assert response.json()["detail"] == "Order service configuration error"
    assert deployment_response.json()["current_version"] == "v1.1.0"


def test_health_remains_alive_when_missing_configuration_is_active() -> None:
    inject_missing_config_fault()

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "order-service"}


def test_payment_service_is_not_called_when_configuration_is_missing() -> None:
    called = False

    def payment_handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    mock_payment_service(payment_handler)
    inject_missing_config_fault()

    response = client.post("/orders", json={"amount": 99.9})

    assert response.status_code == 500
    assert not called


def test_request_id_is_preserved_when_configuration_is_missing() -> None:
    inject_missing_config_fault()

    response = client.post(
        "/orders",
        json={"amount": 99.9},
        headers={REQUEST_ID_HEADER: "missing-config-test"},
    )

    assert response.status_code == 500
    assert response.headers[REQUEST_ID_HEADER] == "missing-config-test"


def test_reset_recovers_from_missing_configuration() -> None:
    def payment_handler(request: httpx.Request) -> httpx.Response:
        order_id = json.loads(request.content)["order_id"]
        return httpx.Response(
            200,
            json={"payment_id": "pay-123", "order_id": order_id, "status": "approved"},
        )

    mock_payment_service(payment_handler)
    inject_missing_config_fault()

    failed_response = client.post("/orders", json={"amount": 99.9})
    reset_fault_lab()
    recovered_response = client.post("/orders", json={"amount": 99.9})
    deployment_response = client.get("/internal/deployment")

    assert failed_response.status_code == 500
    assert recovered_response.status_code == 200
    assert recovered_response.json()["status"] == "confirmed"
    assert deployment_response.json()["current_version"] == "v1.0.0"


def test_metrics_reflect_configuration_failure_without_exposing_diagnosis() -> None:
    inject_missing_config_fault()

    failed_response = client.post("/orders", json={"amount": 99.9})
    metrics_response = client.get("/internal/metrics")
    metrics = metrics_response.json()

    assert failed_response.status_code == 500
    assert metrics_response.status_code == 200
    assert metrics["service"] == "order-service"
    assert metrics["request_count"] == 1
    assert metrics["success_count"] == 0
    assert metrics["error_count"] == 1
    assert "fault_name" not in metrics
    assert "root_cause" not in metrics
    assert "expected_answer" not in metrics


def test_internal_logs_returns_real_missing_configuration_event_without_cheat_fields() -> None:
    inject_missing_config_fault()
    started_at = datetime.now(UTC)

    order_response = client.post("/orders", json={"amount": 99.9})
    logs_response = client.get(
        "/internal/logs",
        params={
            "time_range_start": (started_at - timedelta(seconds=1)).isoformat(),
            "time_range_end": (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
            "level": "error",
            "limit": 10,
        },
    )

    assert order_response.status_code == 500
    assert logs_response.status_code == 200
    logs = logs_response.json()
    assert logs["service"] == "order-service"
    assert logs["match_count"] >= 1
    event = next(
        event
        for event in logs["events"]
        if event["error_type"] == "MissingRequiredConfiguration"
    )
    assert event["request_id"]
    assert not {"fault_name", "root_cause", "expected_answer", "recommended_action"} & event.keys()


def test_internal_logs_filters_real_timeout_events_and_is_bounded() -> None:
    def payment_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("payment-service timed out", request=request)

    mock_payment_service(payment_handler)
    started_at = datetime.now(UTC)
    order_response = client.post("/orders", json={"amount": 99.9})
    logs_response = client.get(
        "/internal/logs",
        params={
            "time_range_start": (started_at - timedelta(seconds=1)).isoformat(),
            "time_range_end": (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
            "level": "error",
            "query": "payment_request_failed",
            "limit": 1,
        },
    )

    assert order_response.status_code == 502
    assert logs_response.status_code == 200
    logs = logs_response.json()
    assert logs["match_count"] == 1
    assert len(logs["events"]) == 1
    assert logs["events"][0]["message"] == "payment_request_failed"
    assert logs["events"][0]["error_type"] == "ReadTimeout"


def test_missing_config_fault_records_deployment_transition_without_leaking_fault_details() -> None:
    inject_missing_config_fault()

    response = client.get("/internal/deployment")
    deployment = response.json()

    assert response.status_code == 200
    assert deployment["service"] == "order-service"
    assert deployment["current_version"] == "v1.1.0"
    assert deployment["previous_version"] == "v1.0.0"
    deployed_at = datetime.fromisoformat(deployment["deployed_at"])
    assert deployed_at.tzinfo is not None
    assert deployed_at.utcoffset() == UTC.utcoffset(deployed_at)
    assert not {"fault_name", "root_cause", "config_key", "action"} & deployment.keys()


def test_reset_restores_deployment_baseline_after_missing_config_fault() -> None:
    inject_missing_config_fault()
    reset_fault_lab()

    response = client.get("/internal/deployment")

    assert response.status_code == 200
    assert response.json() == {
        "service": "order-service",
        "current_version": "v1.0.0",
        "previous_version": None,
        "deployed_at": None,
    }
