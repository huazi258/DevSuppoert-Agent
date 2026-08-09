from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from devsupport_backend.database import engine, get_session
from devsupport_backend.main import app
from devsupport_backend.models import Incident


@pytest.fixture
def database_session() -> Iterator[Session]:
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def api_client(database_session: Session) -> Iterator[TestClient]:
    def override_get_session() -> Iterator[Session]:
        yield database_session

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def incident_payload() -> dict[str, str]:
    return {
        "service": "order-service",
        "environment": "local",
        "description": "POST /orders returns 500 after deployment.",
        "time_range_start": "2026-08-08T10:00:00+00:00",
        "time_range_end": "2026-08-08T10:05:00+00:00",
    }


def create_incident(api_client: TestClient, payload: dict[str, str]) -> dict[str, str]:
    response = api_client.post("/incidents", json=payload)

    assert response.status_code == 201
    return response.json()


def test_create_incident_persists_open_record(
    api_client: TestClient, database_session: Session, incident_payload: dict[str, str]
) -> None:
    body = create_incident(api_client, incident_payload)
    stored = database_session.get(Incident, UUID(str(body["id"])))

    assert stored is not None
    assert stored.status == "OPEN"
    assert stored.thread_id == body["thread_id"]
    assert body["thread_id"]
    assert body["service"] == incident_payload["service"]
    assert datetime.fromisoformat(str(body["time_range_start"])) == datetime.fromisoformat(
        incident_payload["time_range_start"]
    )


def test_get_incident_returns_persisted_record(
    api_client: TestClient, incident_payload: dict[str, str]
) -> None:
    created = create_incident(api_client, incident_payload)

    response = api_client.get(f"/incidents/{created['id']}")

    assert response.status_code == 200
    assert response.json() == created
    assert response.json()["thread_id"] == created["thread_id"]


def test_get_missing_incident_returns_not_found(api_client: TestClient) -> None:
    response = api_client.get(f"/incidents/{uuid4()}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Incident not found"}


def test_list_incidents_returns_created_record(
    api_client: TestClient, incident_payload: dict[str, str]
) -> None:
    created = create_incident(api_client, incident_payload)

    response = api_client.get("/incidents")

    assert response.status_code == 200
    assert response.json() == [created]


def test_incident_rejects_invalid_time_range(
    api_client: TestClient, incident_payload: dict[str, str]
) -> None:
    incident_payload["time_range_start"] = "2026-08-08T10:05:00+00:00"
    incident_payload["time_range_end"] = "2026-08-08T10:00:00+00:00"

    response = api_client.post("/incidents", json=incident_payload)

    assert response.status_code == 422


@pytest.mark.parametrize("field_name", ["service", "environment", "description"])
def test_incident_rejects_blank_required_text(
    api_client: TestClient, incident_payload: dict[str, str], field_name: str
) -> None:
    incident_payload[field_name] = "   "

    response = api_client.post("/incidents", json=incident_payload)

    assert response.status_code == 422


def test_incident_rejects_timezone_naive_time(
    api_client: TestClient, incident_payload: dict[str, str]
) -> None:
    incident_payload["time_range_start"] = "2026-08-08T10:00:00"

    response = api_client.post("/incidents", json=incident_payload)

    assert response.status_code == 422


def test_database_session_fixture_rolls_back_each_test(database_session: Session) -> None:
    now = datetime.now(UTC)
    incident = Incident(
        service="fixture-check",
        environment="test",
        description="This record is rolled back after the test.",
        time_range_start=now,
        time_range_end=now + timedelta(minutes=1),
        thread_id=str(uuid4()),
    )
    database_session.add(incident)
    database_session.flush()

    assert database_session.scalar(select(Incident).where(Incident.id == incident.id)) is incident
