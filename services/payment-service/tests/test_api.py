from fastapi.testclient import TestClient

from payment_service.main import REQUEST_ID_HEADER, app

client = TestClient(app)


def test_health_check_returns_service_identity() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "payment-service"}


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
