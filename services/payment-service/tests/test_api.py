from datetime import UTC, datetime, timedelta
from time import perf_counter

import pytest
from fastapi.testclient import TestClient

from payment_service.main import REQUEST_ID_HEADER, app, log_buffer, telemetry
from payment_service.runtime_state import inject_payment_timeout, reset_runtime_state

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_fault_lab_state() -> None:
    reset_runtime_state()
    log_buffer.clear()
    telemetry.span_buffer.clear()
    yield
    log_buffer.clear()
    telemetry.span_buffer.clear()
    reset_runtime_state()


def test_health_check_returns_service_identity() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "payment-service"}


def test_internal_traces_returns_real_ended_payment_span() -> None:
    started_at = datetime.now(UTC)
    payment_response = client.post("/payments", json={"order_id": "order-123", "amount": 99.9})
    traces_response = client.get(
        "/internal/traces",
        params={
            "time_range_start": (started_at - timedelta(seconds=1)).isoformat(),
            "time_range_end": (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
            "limit": 20,
        },
    )

    assert payment_response.status_code == 200
    assert traces_response.status_code == 200
    payment_span = next(
        span
        for span in traces_response.json()["spans"]
        if span["operation"] == "POST /payments"
    )
    assert payment_span["service"] == "payment-service"
    assert payment_span["trace_id"]
    assert payment_span["duration_ms"] >= 0


def test_deployment_reports_stable_payment_service_version() -> None:
    response = client.get("/internal/deployment")

    assert response.status_code == 200
    assert response.json() == {
        "service": "payment-service",
        "current_version": "v1.0.0",
        "previous_version": None,
        "deployed_at": None,
    }


def test_create_payment_approves_valid_request() -> None:
    response = client.post("/payments", json={"order_id": "order-123", "amount": 99.9})

    body = response.json()
    assert response.status_code == 200
    assert body["payment_id"].startswith("pay-")
    assert body["order_id"] == "order-123"
    assert body["status"] == "approved"


def test_create_payment_rejects_non_positive_amount() -> None:
    response = client.post("/payments", json={"order_id": "order-123", "amount": 0})

    assert response.status_code == 422


def test_create_payment_rejects_blank_order_id() -> None:
    response = client.post("/payments", json={"order_id": "   ", "amount": 99.9})

    assert response.status_code == 422


def test_request_id_is_preserved_in_response() -> None:
    request_id = "upstream-request-123"

    response = client.get("/health", headers={REQUEST_ID_HEADER: request_id})

    assert response.headers[REQUEST_ID_HEADER] == request_id


def test_payment_timeout_fault_adds_real_response_delay() -> None:
    inject_payment_timeout(delay_seconds=0.05)

    started_at = perf_counter()
    response = client.post("/payments", json={"order_id": "order-123", "amount": 99.9})
    elapsed_seconds = perf_counter() - started_at

    assert response.status_code == 200
    assert elapsed_seconds >= 0.04


def test_payment_timeout_fault_does_not_change_deployment_state() -> None:
    before = client.get("/internal/deployment").json()
    inject_payment_timeout(delay_seconds=0.05)
    after = client.get("/internal/deployment").json()

    assert after == before


def test_reset_removes_payment_timeout_delay() -> None:
    inject_payment_timeout(delay_seconds=0.05)
    client.post("/payments", json={"order_id": "order-123", "amount": 99.9})
    reset_runtime_state()

    started_at = perf_counter()
    response = client.post("/payments", json={"order_id": "order-123", "amount": 99.9})

    assert response.status_code == 200
    assert perf_counter() - started_at < 0.04


def test_health_remains_available_when_payment_timeout_is_active() -> None:
    inject_payment_timeout(delay_seconds=0.05)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "payment-service"}


def test_metrics_capture_delayed_payment_without_exposing_diagnosis() -> None:
    inject_payment_timeout(delay_seconds=0.05)

    payment_response = client.post("/payments", json={"order_id": "order-123", "amount": 99.9})
    metrics_response = client.get("/internal/metrics")
    metrics = metrics_response.json()

    assert payment_response.status_code == 200
    assert metrics_response.status_code == 200
    assert metrics["request_count"] == 1
    assert metrics["success_count"] == 1
    assert metrics["error_count"] == 0
    assert metrics["last_request_duration_ms"] >= 40
    assert metrics["average_request_duration_ms"] >= 40
    assert "fault_name" not in metrics
    assert "root_cause" not in metrics
    assert "expected_answer" not in metrics


def test_internal_logs_returns_bounded_structured_stdout_events() -> None:
    started_at = datetime.now(UTC)
    payment_response = client.post("/payments", json={"order_id": "order-123", "amount": 99.9})
    logs_response = client.get(
        "/internal/logs",
        params={
            "time_range_start": (started_at - timedelta(seconds=1)).isoformat(),
            "time_range_end": (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
            "level": "info",
            "limit": 1,
        },
    )

    assert payment_response.status_code == 200
    assert logs_response.status_code == 200
    logs = logs_response.json()
    assert logs["service"] == "payment-service"
    assert logs["match_count"] == 1
    assert len(logs["events"]) == 1
    assert logs["events"][0]["message"] == "http_request"
    assert not {"fault_name", "root_cause", "expected_answer", "recommended_action"} & logs[
        "events"
    ][0].keys()
