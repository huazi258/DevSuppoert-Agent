import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

import order_service.fault_control as fault_control
from order_service.deployment_state import reset_deployment_state
from order_service.fault_control import inject_missing_config_fault
from order_service.main import (
    INTERNAL_FAULT_LAB_RESET_PATH,
    REQUEST_ID_HEADER,
    app,
    get_payment_client,
    log_buffer,
    telemetry,
)
from order_service.runtime_state import reset_runtime_state

client = TestClient(app)


@pytest.fixture(autouse=True)
def clear_dependency_overrides() -> None:
    async def default_payment_client():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(500, request=request)),
            base_url="http://payment-service.test",
        ) as payment_client:
            yield payment_client

    reset_runtime_state()
    reset_deployment_state()
    log_buffer.clear()
    telemetry.span_buffer.clear()
    app.dependency_overrides.clear()
    app.dependency_overrides[get_payment_client] = default_payment_client
    yield
    app.dependency_overrides.clear()
    log_buffer.clear()
    telemetry.span_buffer.clear()
    reset_runtime_state()
    reset_deployment_state()


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


def test_rollback_only_allows_previous_version_and_preserves_fault_evidence() -> None:
    def payment_handler(request: httpx.Request) -> httpx.Response:
        payment = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "payment_id": "payment-recovered",
                "order_id": payment["order_id"],
                "status": "approved",
            },
            request=request,
        )

    mock_payment_service(payment_handler)
    inject_missing_config_fault()

    failed_response = client.post("/orders", json={"amount": 99.9})
    metrics_before = client.get("/internal/metrics").json()
    rollback_response = client.post(
        "/internal/deployment/rollback", json={"target_version": "v1.0.0"}
    )
    recovered_response = client.post("/orders", json={"amount": 99.9})
    deployment = client.get("/internal/deployment").json()
    metrics_after = client.get("/internal/metrics").json()

    assert failed_response.status_code == 500
    assert rollback_response.status_code == 200
    assert rollback_response.json()["executed"] is True
    assert deployment["current_version"] == "v1.0.0"
    assert deployment["previous_version"] == "v1.1.0"
    assert deployment["deployed_at"] is not None
    assert recovered_response.status_code == 200
    assert metrics_after["error_count"] == metrics_before["error_count"]
    assert metrics_after["request_count"] > metrics_before["request_count"]


def test_rollback_rejects_versions_other_than_current_previous_deployment() -> None:
    inject_missing_config_fault()

    response = client.post("/internal/deployment/rollback", json={"target_version": "v9.9.9"})
    deployment = client.get("/internal/deployment").json()

    assert response.status_code == 409
    assert deployment["current_version"] == "v1.1.0"
    assert deployment["previous_version"] == "v1.0.0"


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
    reset_response = client.post(INTERNAL_FAULT_LAB_RESET_PATH)
    recovered_response = client.post("/orders", json={"amount": 99.9})
    deployment_response = client.get("/internal/deployment")

    assert failed_response.status_code == 500
    assert reset_response.status_code == 200
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
    reset_response = client.post(INTERNAL_FAULT_LAB_RESET_PATH)

    response = client.get("/internal/deployment")

    assert reset_response.status_code == 200
    assert response.status_code == 200
    assert response.json() == {
        "service": "order-service",
        "current_version": "v1.0.0",
        "previous_version": None,
        "deployed_at": None,
    }


def test_internal_fault_lab_reset_clears_order_observability_and_runtime_state() -> None:
    def payment_handler(request: httpx.Request) -> httpx.Response:
        order_id = json.loads(request.content)["order_id"]
        return httpx.Response(
            200,
            json={"payment_id": "pay-123", "order_id": order_id, "status": "approved"},
        )

    mock_payment_service(payment_handler)
    started_at = datetime.now(UTC)
    inject_missing_config_fault()

    failed_response = client.post("/orders", json={"amount": 99.9})
    before_logs = log_buffer.query(
        time_range_start=started_at - timedelta(seconds=1),
        time_range_end=datetime.now(UTC) + timedelta(seconds=1),
        level="error",
        query="MissingRequiredConfiguration",
        limit=10,
    )
    before_traces = telemetry.span_buffer.query(
        time_range_start=started_at - timedelta(seconds=1),
        time_range_end=datetime.now(UTC) + timedelta(seconds=1),
        trace_id=None,
        limit=100,
    )

    reset_response = client.post(INTERNAL_FAULT_LAB_RESET_PATH)
    after_logs = log_buffer.query(
        time_range_start=started_at - timedelta(seconds=1),
        time_range_end=datetime.now(UTC) + timedelta(seconds=1),
        level=None,
        query=None,
        limit=100,
    )
    after_traces = telemetry.span_buffer.query(
        time_range_start=started_at - timedelta(seconds=1),
        time_range_end=datetime.now(UTC) + timedelta(seconds=1),
        trace_id=None,
        limit=100,
    )
    metrics_response = client.get("/internal/metrics")
    deployment_response = client.get("/internal/deployment")
    recovered_response = client.post("/orders", json={"amount": 99.9})

    assert failed_response.status_code == 500
    assert before_logs[0] == 1
    assert any("POST /orders" in span["operation"] for span in before_traces[1])
    assert reset_response.status_code == 200
    assert reset_response.json() == {"service": "order-service", "status": "reset"}
    assert after_logs == (0, [])
    assert after_traces == (0, [])
    assert metrics_response.json()["request_count"] == 0
    assert metrics_response.json()["error_count"] == 0
    assert deployment_response.json()["current_version"] == "v1.0.0"
    assert deployment_response.json()["previous_version"] is None
    assert recovered_response.status_code == 200


def test_cli_reset_calls_both_fault_lab_services(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            service = (
                "order-service"
                if calls[-1].startswith("http://127.0.0.1:8000")
                else "payment-service"
            )
            return {"service": service, "status": "reset"}

    def post(url: str, **_: object) -> Response:
        calls.append(url)
        return Response()

    monkeypatch.setattr(fault_control.httpx, "post", post)

    fault_control.reset_fault_lab()

    assert calls == [
        "http://127.0.0.1:8000/internal/fault-lab/reset",
        "http://127.0.0.1:8001/internal/fault-lab/reset",
    ]


def test_cli_reset_fails_when_one_service_reset_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def __init__(self, service: str) -> None:
            self._service = service

        def raise_for_status(self) -> None:
            if self._service == "payment-service":
                raise RuntimeError("payment reset unavailable")

        def json(self) -> dict[str, str]:
            return {"service": self._service, "status": "reset"}

    def post(url: str, **_: object) -> Response:
        service = "order-service" if ":8000" in url else "payment-service"
        return Response(service)

    monkeypatch.setattr(fault_control.httpx, "post", post)

    with pytest.raises(RuntimeError, match="payment-service"):
        fault_control.reset_fault_lab()
