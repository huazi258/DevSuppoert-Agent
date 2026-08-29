from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from devsupport_backend.agent.runtime import WorkflowFailure
from devsupport_backend.agent.state import (
    ActionExecutionOutcome,
    AgentStage,
    ApprovalOutcome,
    ApprovalStatus,
    EvidenceContext,
    FailureCategory,
    PolicyDecision,
    PolicyOutcome,
    PolicyReasonCode,
    VerificationOutcome,
    VerificationStatus,
    create_initial_agent_state,
)
from devsupport_backend.database import engine, get_session
from devsupport_backend.main import app
from devsupport_backend.models import Action, Approval, Incident, Report
from devsupport_backend.routers.incidents import get_workflow_runtime
from devsupport_backend.tools.schemas import ToolStatus


class FakeWorkflowRuntime:
    def __init__(
        self,
        *,
        start_error: Exception | None = None,
        get_state_results: list[object | None] | None = None,
        failure: WorkflowFailure | None = None,
        failure_error: Exception | None = None,
        retry_error: Exception | None = None,
    ) -> None:
        self.states: dict[str, object] = {}
        self.start_error = start_error
        self.get_state_results = get_state_results or []
        self.failure = failure
        self.failure_error = failure_error
        self.retry_error = retry_error
        self.start_calls = 0
        self.started_threads: list[str] = []
        self.retry_calls = 0
        self.retry_usage_calls = 0
        self.retried_threads: list[str] = []

    def get_state(self, thread_id: str):
        if self.get_state_results:
            state = self.get_state_results.pop(0)
            if isinstance(state, Exception):
                raise state
            return state
        return self.states.get(thread_id)

    def start(self, incident: Incident):
        self.start_calls += 1
        self.started_threads.append(incident.thread_id)
        if self.start_error:
            raise self.start_error
        state = create_initial_agent_state(incident)
        state.update(
            {
                "current_stage": AgentStage.RETRIEVAL,
                "evidence": [
                    EvidenceContext(
                        evidence_type="metric_snapshot",
                        source="query_metrics",
                        summary="A compact public evidence summary.",
                        data={"internal": "not-for-web"},
                    )
                ],
            }
        )
        self.states[incident.thread_id] = state
        return state

    def get_failure(self, thread_id: str) -> WorkflowFailure | None:
        if self.failure_error is not None:
            raise self.failure_error
        return self.failure

    def retry_failed_task(self, thread_id: str):
        self.retry_calls += 1
        self.retried_threads.append(thread_id)
        if self.retry_error is not None:
            raise self.retry_error
        self.failure = None
        return self.states[thread_id]

    def record_retry_attempt(self, thread_id: str) -> None:
        self.retry_usage_calls += 1
        state = self.states[thread_id]
        state["workflow_retry_count"] += 1


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


def workflow_client(api_client: TestClient, runtime: FakeWorkflowRuntime) -> TestClient:
    app.dependency_overrides[get_workflow_runtime] = lambda: runtime
    return api_client


def _prepare_retryable_incident(
    api_client: TestClient,
    database_session: Session,
    payload: dict[str, str],
    runtime: FakeWorkflowRuntime,
    *,
    status: str = "INVESTIGATING",
    failed_node: str = "investigation_planning",
) -> Incident:
    created = create_incident(api_client, payload)
    incident = database_session.get(Incident, UUID(created["id"]))
    assert incident is not None
    incident.status = status
    database_session.commit()
    state = create_initial_agent_state(incident)
    state["current_stage"] = AgentStage.INVESTIGATION_PLANNING
    runtime.states[incident.thread_id] = state
    runtime.failure = WorkflowFailure(
        failed_node=failed_node,
        safe_error="Persisted workflow task failed",
        category=FailureCategory.LLM_PROVIDER_TIMEOUT,
        retryable=True,
    )
    return incident


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


def test_workflow_api_start_and_read_use_the_persisted_thread(
    api_client: TestClient, database_session: Session, incident_payload: dict[str, str]
) -> None:
    runtime = FakeWorkflowRuntime()
    client = workflow_client(api_client, runtime)
    created = create_incident(client, incident_payload)

    started = client.post(f"/incidents/{created['id']}/workflow")
    read = client.get(f"/incidents/{created['id']}/workflow")

    incident = database_session.get(Incident, UUID(created["id"]))
    assert started.status_code == 200
    assert read.status_code == 200
    assert incident is not None and incident.status == "INVESTIGATING"
    assert runtime.started_threads == [created["thread_id"]]
    assert read.json()["incident_status"] == "INVESTIGATING"
    assert read.json()["evidence"][0].get("data") is None
    assert "data" not in read.json()["evidence"][0]


def test_workflow_api_unknown_and_not_started_responses(
    api_client: TestClient, incident_payload: dict[str, str]
) -> None:
    runtime = FakeWorkflowRuntime()
    client = workflow_client(api_client, runtime)
    created = create_incident(client, incident_payload)

    unknown_post = client.post(f"/incidents/{uuid4()}/workflow")
    unknown_get = client.get(f"/incidents/{uuid4()}/workflow")
    before_start = client.get(f"/incidents/{created['id']}/workflow")

    assert unknown_post.json() == {"detail": "Incident not found"}
    assert unknown_get.json() == {"detail": "Incident not found"}
    assert before_start.json() == {"detail": "Workflow not started"}
    assert unknown_post.status_code == unknown_get.status_code == before_start.status_code == 404


def test_workflow_api_duplicate_and_non_open_start_are_conflicts(
    api_client: TestClient, database_session: Session, incident_payload: dict[str, str]
) -> None:
    runtime = FakeWorkflowRuntime()
    client = workflow_client(api_client, runtime)
    created = create_incident(client, incident_payload)
    incident = database_session.get(Incident, UUID(created["id"]))
    assert incident is not None
    runtime.states[incident.thread_id] = create_initial_agent_state(incident)

    duplicate = client.post(f"/incidents/{incident.id}/workflow")
    incident.status = "INVESTIGATING"
    runtime.failure = WorkflowFailure(
        failed_node="investigation_planning",
        safe_error="Persisted workflow task failed",
        category=FailureCategory.LLM_PROVIDER_TIMEOUT,
        retryable=True,
    )
    database_session.commit()
    investigating = client.post(f"/incidents/{incident.id}/workflow")
    incident.status = "WAITING_APPROVAL"
    database_session.commit()
    waiting = client.post(f"/incidents/{incident.id}/workflow")
    incident.status = "RESOLVED"
    database_session.commit()
    resolved = client.post(f"/incidents/{incident.id}/workflow")

    assert {
        duplicate.status_code,
        investigating.status_code,
        waiting.status_code,
        resolved.status_code,
    } == {409}
    assert runtime.start_calls == 0


def test_workflow_retry_api_retries_eligible_failure_on_original_thread(
    api_client: TestClient,
    database_session: Session,
    incident_payload: dict[str, str],
) -> None:
    runtime = FakeWorkflowRuntime()
    client = workflow_client(api_client, runtime)
    incident = _prepare_retryable_incident(client, database_session, incident_payload, runtime)

    response = client.post(f"/incidents/{incident.id}/workflow/retry")

    database_session.refresh(incident)
    assert response.status_code == 200
    assert response.json()["incident_id"] == str(incident.id)
    assert response.json()["retry_available"] is False
    assert incident.thread_id == runtime.retried_threads[0]
    assert runtime.retry_calls == 1
    assert runtime.retried_threads == [incident.thread_id]
    assert runtime.start_calls == 0
    assert database_session.scalar(
        select(func.count()).select_from(Action).where(Action.incident_id == incident.id)
    ) == 0
    assert database_session.scalar(
        select(func.count()).select_from(Approval).where(Approval.incident_id == incident.id)
    ) == 0


def test_workflow_retry_api_returns_503_and_preserves_retryable_failure(
    api_client: TestClient,
    database_session: Session,
    incident_payload: dict[str, str],
) -> None:
    runtime = FakeWorkflowRuntime(retry_error=RuntimeError("provider returned empty content"))
    client = workflow_client(api_client, runtime)
    incident = _prepare_retryable_incident(client, database_session, incident_payload, runtime)

    retried = client.post(f"/incidents/{incident.id}/workflow/retry")
    refreshed = client.get(f"/incidents/{incident.id}/workflow")

    database_session.refresh(incident)
    assert retried.status_code == 503
    assert retried.json() == {"detail": "Workflow retry failed"}
    assert refreshed.status_code == 200
    assert refreshed.json()["retry_available"] is False
    assert runtime.retry_usage_calls == 1
    assert incident.status == "INVESTIGATING"
    assert runtime.retried_threads == [incident.thread_id]
    assert runtime.start_calls == 0
    assert database_session.scalar(
        select(func.count()).select_from(Action).where(Action.incident_id == incident.id)
    ) == 0
    assert database_session.scalar(
        select(func.count()).select_from(Approval).where(Approval.incident_id == incident.id)
    ) == 0


def test_workflow_retry_api_returns_503_when_failure_metadata_cannot_be_read(
    api_client: TestClient,
    database_session: Session,
    incident_payload: dict[str, str],
) -> None:
    runtime = FakeWorkflowRuntime(failure_error=RuntimeError("checkpoint unavailable"))
    client = workflow_client(api_client, runtime)
    incident = _prepare_retryable_incident(client, database_session, incident_payload, runtime)

    retried = client.post(f"/incidents/{incident.id}/workflow/retry")
    refreshed = client.get(f"/incidents/{incident.id}/workflow")

    database_session.refresh(incident)
    assert retried.status_code == 503
    assert retried.json() == {"detail": "Workflow retry failed"}
    assert refreshed.status_code == 200
    assert refreshed.json()["retry_available"] is False
    assert incident.status == "INVESTIGATING"
    assert runtime.retry_calls == 0


@pytest.mark.parametrize(
    "case",
    [
        "unknown",
        "open",
        "no_checkpoint",
        "no_failure",
        "policy_gate",
        "approval_wait",
        "controlled_action_execution",
        "waiting_approval",
        "resolved",
        "needs_manual_action",
        "action",
        "approval",
        "approval_outcome",
        "execution_outcome",
        "verification_outcome",
    ],
)
def test_workflow_retry_api_rejects_ineligible_lifecycle_and_persistence_states(
    api_client: TestClient,
    database_session: Session,
    incident_payload: dict[str, str],
    case: str,
) -> None:
    runtime = FakeWorkflowRuntime()
    client = workflow_client(api_client, runtime)
    if case == "unknown":
        response = client.post(f"/incidents/{uuid4()}/workflow/retry")
        assert response.status_code == 404
        assert runtime.retry_calls == 0
        return

    if case == "no_checkpoint":
        created = create_incident(client, incident_payload)
        incident = database_session.get(Incident, UUID(created["id"]))
        assert incident is not None
        incident.status = "INVESTIGATING"
        database_session.commit()
    else:
        status_for_case = {
            "open": "OPEN",
            "waiting_approval": "WAITING_APPROVAL",
            "resolved": "RESOLVED",
            "needs_manual_action": "NEEDS_MANUAL_ACTION",
        }.get(case, "INVESTIGATING")
        failed_node = case if case in {
            "policy_gate",
            "approval_wait",
            "controlled_action_execution",
        } else "investigation_planning"
        incident = _prepare_retryable_incident(
            client,
            database_session,
            incident_payload,
            runtime,
            status=status_for_case,
            failed_node=failed_node,
        )

    if case == "no_failure":
        runtime.failure = None
    elif case in {"action", "approval"}:
        action = Action(
            incident_id=incident.id,
            action_type="rollback_deployment",
            status="PENDING_APPROVAL",
            parameters={},
        )
        database_session.add(action)
        database_session.commit()
        if case == "approval":
            database_session.add(
                Approval(incident_id=incident.id, action_id=action.id, status="APPROVED")
            )
            database_session.commit()
    elif case == "execution_outcome":
        runtime.states[incident.thread_id]["execution_outcome"] = ActionExecutionOutcome(
            status=ToolStatus.FAILURE,
            executed=False,
        )
    elif case == "approval_outcome":
        runtime.states[incident.thread_id]["approval_outcome"] = ApprovalOutcome(
            approval_id=uuid4(),
            action_id=uuid4(),
            status=ApprovalStatus.APPROVED,
        )
    elif case == "verification_outcome":
        runtime.states[incident.thread_id]["verification_outcome"] = VerificationOutcome(
            status=VerificationStatus.INCONCLUSIVE,
            summary="Verification state already exists.",
        )

    response = client.post(f"/incidents/{incident.id}/workflow/retry")

    assert response.status_code == 409
    assert runtime.retry_calls == 0


def test_workflow_retry_api_rejects_sequential_duplicate_after_successful_advance(
    api_client: TestClient,
    database_session: Session,
    incident_payload: dict[str, str],
) -> None:
    runtime = FakeWorkflowRuntime()
    client = workflow_client(api_client, runtime)
    incident = _prepare_retryable_incident(client, database_session, incident_payload, runtime)

    first = client.post(f"/incidents/{incident.id}/workflow/retry")
    second = client.post(f"/incidents/{incident.id}/workflow/retry")

    assert first.status_code == 200
    assert second.status_code == 409
    assert runtime.retry_calls == 1


def test_workflow_get_is_read_only(
    api_client: TestClient, database_session: Session, incident_payload: dict[str, str]
) -> None:
    runtime = FakeWorkflowRuntime()
    client = workflow_client(api_client, runtime)
    created = create_incident(client, incident_payload)
    incident = database_session.get(Incident, UUID(created["id"]))
    assert incident is not None
    runtime.states[incident.thread_id] = create_initial_agent_state(incident)
    before_updated = incident.updated_at
    reports_before = database_session.scalar(
        select(func.count()).select_from(Report).where(Report.incident_id == incident.id)
    )
    actions_before = database_session.scalar(
        select(func.count()).select_from(Action).where(Action.incident_id == incident.id)
    )

    response = client.get(f"/incidents/{incident.id}/workflow")

    database_session.refresh(incident)
    assert response.status_code == 200
    assert incident.updated_at == before_updated
    assert (
        database_session.scalar(
            select(func.count()).select_from(Report).where(Report.incident_id == incident.id)
        )
        == reports_before
    )
    assert (
        database_session.scalar(
            select(func.count()).select_from(Action).where(Action.incident_id == incident.id)
        )
        == actions_before
    )
    assert runtime.start_calls == 0


def test_workflow_start_failure_restores_open(
    api_client: TestClient, database_session: Session, incident_payload: dict[str, str]
) -> None:
    runtime = FakeWorkflowRuntime(start_error=RuntimeError("provider unavailable"))
    client = workflow_client(api_client, runtime)
    created = create_incident(client, incident_payload)

    response = client.post(f"/incidents/{created['id']}/workflow")

    incident = database_session.get(Incident, UUID(created["id"]))
    assert response.status_code == 503
    assert incident is not None and incident.status == "OPEN"


def test_workflow_get_invalid_persisted_action_parameters_returns_conflict(
    api_client: TestClient, database_session: Session, incident_payload: dict[str, str]
) -> None:
    runtime = FakeWorkflowRuntime()
    client = workflow_client(api_client, runtime)
    created = create_incident(client, incident_payload)
    incident = database_session.get(Incident, UUID(created["id"]))
    assert incident is not None
    action = Action(
        incident_id=incident.id,
        action_type="rollback_deployment",
        status="PENDING_APPROVAL",
        parameters={
            "service": "order-service",
            "environment": "local",
            "current_version": "v1.1.0",
            "reason": "Verified deployment facts require rollback.",
        },
    )
    database_session.add(action)
    database_session.commit()
    state = create_initial_agent_state(incident)
    state["policy_outcome"] = PolicyOutcome(
        decision=PolicyDecision.APPROVAL_REQUIRED,
        reason_code=PolicyReasonCode.APPROVAL_REQUIRED,
        reason="Verified action requires approval.",
        action_id=action.id,
    )
    runtime.states[incident.thread_id] = state

    response = client.get(f"/incidents/{incident.id}/workflow")

    assert response.status_code == 409
    assert response.json() == {"detail": "Persisted Action parameters are invalid"}


def test_workflow_start_reconciliation_read_failure_preserves_investigating(
    api_client: TestClient, database_session: Session, incident_payload: dict[str, str]
) -> None:
    runtime = FakeWorkflowRuntime(
        start_error=RuntimeError("provider unavailable"),
        get_state_results=[None, RuntimeError("checkpoint unavailable")],
    )
    client = workflow_client(api_client, runtime)
    created = create_incident(client, incident_payload)

    response = client.post(f"/incidents/{created['id']}/workflow")

    incident = database_session.get(Incident, UUID(created["id"]))
    assert response.status_code == 503
    assert response.json() == {"detail": "Workflow start failed"}
    assert incident is not None and incident.status == "INVESTIGATING"


@pytest.mark.parametrize("origin", ["http://localhost:3000", "http://127.0.0.1:3000"])
def test_workflow_cors_allows_only_local_nextjs_origins(origin: str) -> None:
    allowed = TestClient(app).options(
        "/incidents",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
        },
    )
    rejected = TestClient(app).options(
        "/incidents",
        headers={
            "Origin": "http://example.invalid",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert allowed.headers["access-control-allow-origin"] == origin
    assert "access-control-allow-origin" not in rejected.headers


def test_get_final_report_is_read_only_and_distinguishes_missing_records(
    api_client: TestClient, database_session: Session, incident_payload: dict[str, str]
) -> None:
    created = create_incident(api_client, incident_payload)
    incident_id = UUID(created["id"])

    no_report = api_client.get(f"/incidents/{incident_id}/report")
    unknown = api_client.get(f"/incidents/{uuid4()}/report")
    report = Report(
        incident_id=incident_id,
        root_cause="A test root cause.",
        content={"schema_version": "v0", "final_status": "NEEDS_MANUAL_ACTION"},
    )
    database_session.add(report)
    database_session.commit()

    response = api_client.get(f"/incidents/{incident_id}/report")

    assert no_report.status_code == 404
    assert unknown.status_code == 404
    assert response.status_code == 200
    assert response.json()["id"] == str(report.id)
    assert response.json()["content"] == report.content


def test_list_incidents_returns_created_record(
    api_client: TestClient, incident_payload: dict[str, str]
) -> None:
    created = create_incident(api_client, incident_payload)

    response = api_client.get("/incidents")

    assert response.status_code == 200
    assert isinstance(response.json(), list)
    assert [record for record in response.json() if record["id"] == created["id"]] == [created]


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
