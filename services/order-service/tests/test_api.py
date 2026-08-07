import json
from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from order_service.main import REQUEST_ID_HEADER, app, get_payment_client

client = TestClient(app)


@pytest.fixture(autouse=True)
def clear_dependency_overrides() -> None:
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


def mock_payment_service(handler: Callable[[httpx.Request], httpx.Response]) -> None:
    async def get_mock_client():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://payment-service.test"
        ) as payment_client:
            yield payment_client

    app.dependency_overrides[get_payment_client] = get_mock_client


def test_health_check_returns_service_identity() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "order-service"}


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
