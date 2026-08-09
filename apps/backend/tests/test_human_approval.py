"""Task 4.2 coverage for persisted approval interrupts and the decision-only API."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from devsupport_backend.agent.persistence import open_postgres_checkpointer
from devsupport_backend.agent.runtime import WorkflowService
from devsupport_backend.agent.state import (
    ActionType,
    AgentStage,
    AgentState,
    ApprovalOutcome,
    ApprovalStatus,
    PolicyDecision,
    PolicyOutcome,
    PolicyReasonCode,
    create_initial_agent_state,
)
from devsupport_backend.approvals import (
    ApprovalService,
    ApprovalValidationError,
    ApprovalWaitService,
    PostgresWorkflowStateReader,
    approval_interrupt_node,
    approval_wait_node,
)
from devsupport_backend.database import get_session
from devsupport_backend.main import app
from devsupport_backend.models import Action, Approval, Incident
from devsupport_backend.routers.incidents import (
    get_approval_workflow_coordinator,
    get_workflow_state_reader,
)
from devsupport_backend.schemas.approvals import ApprovalDecision

PENDING_APPROVAL = "PENDING_APPROVAL"
WAITING_APPROVAL = "WAITING_APPROVAL"


class StaticWorkflowStateReader:
    """Controlled persisted-checkpoint projection for Approval API validation tests."""

    def __init__(self, state: AgentState) -> None:
        self._state = state
        self.calls: list[str] = []

    def get_state(self, thread_id: str) -> AgentState:
        self.calls.append(thread_id)
        return self._state


class StaticWorkflowCoordinator:
    """Advance the controlled checkpoint projection without exercising Task 4.3 here."""

    def __init__(self, reader: StaticWorkflowStateReader, session: Session) -> None:
        self._reader = reader
        self._session = session
        self.calls: list[str] = []

    def resume(self, thread_id: str) -> AgentState:
        self.calls.append(thread_id)
        state = self._reader.get_state(thread_id)
        policy = state["policy_outcome"]
        assert policy is not None and policy.action_id is not None
        approval = self._session.scalar(
            select(Approval).where(Approval.action_id == policy.action_id)
        )
        assert approval is not None
        state["approval_outcome"] = ApprovalOutcome(
            approval_id=approval.id,
            action_id=approval.action_id,
            status=ApprovalStatus(approval.status),
        )
        state["current_stage"] = (
            AgentStage.ACTION_EXECUTION
            if approval.status == ApprovalStatus.APPROVED.value
            else AgentStage.NEEDS_MANUAL_ACTION
        )
        return state


def _incident(session: Session, *, status: str = WAITING_APPROVAL) -> Incident:
    now = datetime.now(UTC)
    incident = Incident(
        id=uuid4(),
        service="order-service",
        environment="local",
        status=status,
        description="Approval workflow test incident.",
        time_range_start=now,
        time_range_end=now + timedelta(minutes=5),
        thread_id=str(uuid4()),
    )
    session.add(incident)
    session.commit()
    return incident


def _pending_action(session: Session, incident: Incident) -> Action:
    action = Action(
        incident_id=incident.id,
        action_type=ActionType.ROLLBACK_DEPLOYMENT.value,
        status=PENDING_APPROVAL,
        parameters={
            "service": "order-service",
            "environment": "local",
            "current_version": "v1.1.0",
            "target_version": "v1.0.0",
            "reason": "Verified deployment facts support a rollback proposal.",
        },
        executed_at=None,
    )
    session.add(action)
    session.commit()
    return action


def _waiting_state(incident: Incident, action: Action) -> AgentState:
    state = create_initial_agent_state(incident)
    state["current_stage"] = AgentStage.WAITING_APPROVAL
    state["policy_outcome"] = PolicyOutcome(
        decision=PolicyDecision.APPROVAL_REQUIRED,
        reason_code=PolicyReasonCode.APPROVAL_REQUIRED,
        reason="Verified rollback requires human approval.",
        action_id=action.id,
    )
    return state


@pytest.fixture
def approval_api_client(
    database_session: Session,
) -> Iterator[tuple[TestClient, StaticWorkflowStateReader, StaticWorkflowCoordinator]]:
    incident = _incident(database_session)
    action = _pending_action(database_session, incident)
    reader = StaticWorkflowStateReader(_waiting_state(incident, action))
    coordinator = StaticWorkflowCoordinator(reader, database_session)

    def override_get_session() -> Iterator[Session]:
        yield database_session

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_workflow_state_reader] = lambda: reader
    app.dependency_overrides[get_approval_workflow_coordinator] = lambda: coordinator
    with TestClient(app) as client:
        yield client, reader, coordinator
    app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("decision", "expected_status"),
    [
        (ApprovalDecision.APPROVE, ApprovalStatus.APPROVED),
        (ApprovalDecision.REJECT, ApprovalStatus.REJECTED),
    ],
)
def test_approval_api_persists_one_server_bound_final_decision(
    approval_api_client: tuple[TestClient, StaticWorkflowStateReader, StaticWorkflowCoordinator],
    database_session: Session,
    decision: ApprovalDecision,
    expected_status: ApprovalStatus,
) -> None:
    client, reader, coordinator = approval_api_client
    state = reader.get_state("test")
    incident_id = state["incident"].id
    action_id = state["policy_outcome"].action_id if state["policy_outcome"] else None
    assert action_id is not None
    action = database_session.get(Action, action_id)
    assert action is not None
    before_parameters = dict(action.parameters)

    response = client.post(f"/incidents/{incident_id}/approval", json={"decision": decision.value})

    assert response.status_code == 200
    assert response.json()["incident_id"] == str(incident_id)
    assert response.json()["action_id"] == str(action_id)
    assert response.json()["status"] == expected_status.value
    approval = database_session.get(Approval, UUID(response.json()["id"]))
    assert approval is not None
    assert approval.action_id == action_id
    assert approval.status == expected_status.value
    database_session.refresh(action)
    assert action.parameters == before_parameters
    assert action.executed_at is None
    assert reader.calls
    persisted_incident = database_session.get(Incident, incident_id)
    assert persisted_incident is not None
    assert coordinator.calls == [persisted_incident.thread_id]


@pytest.mark.parametrize(
    "payload",
    [
        {"decision": "APPROVE", "action_id": str(uuid4())},
        {"decision": "APPROVE", "target_version": "attacker-version"},
        {"decision": "APPROVE", "service": "payment-service"},
        {"decision": "APPROVE", "approved": True},
        {"approved": True},
    ],
)
def test_approval_api_rejects_forged_client_authorization_fields(
    approval_api_client: tuple[TestClient, StaticWorkflowStateReader, StaticWorkflowCoordinator],
    database_session: Session,
    payload: dict[str, object],
) -> None:
    client, reader, _ = approval_api_client
    incident_id = reader.get_state("test")["incident"].id

    response = client.post(f"/incidents/{incident_id}/approval", json=payload)

    assert response.status_code == 422
    assert database_session.query(Approval).count() == 0


def test_approval_api_fails_closed_for_mismatched_checkpoint_action(
    approval_api_client: tuple[TestClient, StaticWorkflowStateReader, StaticWorkflowCoordinator],
    database_session: Session,
) -> None:
    client, reader, _ = approval_api_client
    state = reader.get_state("test")
    other_incident = _incident(database_session)
    other_action = _pending_action(database_session, other_incident)
    reader._state = _waiting_state(state["incident"], other_action)

    response = client.post(
        f"/incidents/{state['incident'].id}/approval", json={"decision": "APPROVE"}
    )

    assert response.status_code == 409
    assert database_session.query(Approval).count() == 0


def test_approval_api_cannot_approve_another_incidents_action(
    approval_api_client: tuple[TestClient, StaticWorkflowStateReader, StaticWorkflowCoordinator],
    database_session: Session,
) -> None:
    client, reader, _ = approval_api_client
    state = reader.get_state("test")
    other_incident = _incident(database_session)

    response = client.post(f"/incidents/{other_incident.id}/approval", json={"decision": "APPROVE"})

    assert response.status_code == 409
    assert database_session.query(Approval).count() == 0
    assert state["incident"].id != other_incident.id


def test_approval_api_rejects_an_already_executed_action(
    approval_api_client: tuple[TestClient, StaticWorkflowStateReader, StaticWorkflowCoordinator],
    database_session: Session,
) -> None:
    client, reader, _ = approval_api_client
    state = reader.get_state("test")
    outcome = state["policy_outcome"]
    assert outcome is not None and outcome.action_id is not None
    action = database_session.get(Action, outcome.action_id)
    assert action is not None
    action.executed_at = datetime.now(UTC)
    database_session.commit()

    response = client.post(
        f"/incidents/{state['incident'].id}/approval", json={"decision": "APPROVE"}
    )

    assert response.status_code == 409
    assert database_session.query(Approval).count() == 0


def test_duplicate_matching_decision_is_idempotent_and_conflicting_one_is_rejected(
    approval_api_client: tuple[TestClient, StaticWorkflowStateReader, StaticWorkflowCoordinator],
    database_session: Session,
) -> None:
    client, reader, coordinator = approval_api_client
    incident_id = reader.get_state("test")["incident"].id

    first = client.post(f"/incidents/{incident_id}/approval", json={"decision": "APPROVE"})
    repeated = client.post(f"/incidents/{incident_id}/approval", json={"decision": "APPROVE"})
    conflicting = client.post(f"/incidents/{incident_id}/approval", json={"decision": "REJECT"})

    assert first.status_code == 200
    assert repeated.status_code == 200
    assert repeated.json()["id"] == first.json()["id"]
    assert conflicting.status_code == 409
    assert database_session.query(Approval).count() == 1
    assert len(coordinator.calls) == 1


def test_approval_database_constraints_reject_orphans_and_duplicate_actions(
    database_session: Session,
) -> None:
    incident = _incident(database_session)
    action = _pending_action(database_session, incident)
    database_session.add(
        Approval(incident_id=incident.id, action_id=action.id, status=ApprovalStatus.APPROVED.value)
    )
    database_session.commit()

    with pytest.raises(IntegrityError):
        with database_session.begin_nested():
            database_session.add(
                Approval(
                    incident_id=incident.id,
                    action_id=action.id,
                    status=ApprovalStatus.REJECTED.value,
                )
            )
            database_session.flush()
    with pytest.raises(IntegrityError):
        with database_session.begin_nested():
            database_session.add(
                Approval(  # type: ignore[arg-type]
                    incident_id=incident.id,
                    action_id=None,
                    status=ApprovalStatus.APPROVED.value,
                )
            )
            database_session.flush()


def test_policy_outcome_rejects_invalid_action_binding_combinations() -> None:
    with pytest.raises(ValueError, match="must include an action_id"):
        PolicyOutcome(
            decision=PolicyDecision.APPROVAL_REQUIRED,
            reason_code=PolicyReasonCode.APPROVAL_REQUIRED,
            reason="Approval is required.",
        )
    with pytest.raises(ValueError, match="must not include an action_id"):
        PolicyOutcome(
            decision=PolicyDecision.DENIED,
            reason_code=PolicyReasonCode.MANUAL_ACTION,
            reason="Manual action is not executable.",
            action_id=uuid4(),
        )


def _approval_interrupt_graph(
    *, checkpointer: BaseCheckpointSaver, approval_wait: ApprovalWaitService
):
    graph = StateGraph(AgentState)
    graph.add_node("approval_wait", lambda state: approval_wait_node(state, approval_wait))
    graph.add_node(
        "approval_interrupt", lambda state: approval_interrupt_node(state, approval_wait)
    )
    graph.add_edge(START, "approval_wait")
    graph.add_edge("approval_wait", "approval_interrupt")
    graph.add_edge("approval_interrupt", END)
    return graph.compile(checkpointer=checkpointer)


def _delete_thread(thread_id: str) -> None:
    with open_postgres_checkpointer() as checkpointer:
        checkpointer.delete_thread(thread_id)


def test_postgres_interrupt_persists_waiting_state_across_reopen(
    database_session: Session,
) -> None:
    incident = _incident(database_session, status="OPEN")
    action = _pending_action(database_session, incident)
    state = _waiting_state(incident, action)
    state["current_stage"] = AgentStage.POLICY_GATE
    config = WorkflowService.config_for(incident.thread_id)
    try:
        with open_postgres_checkpointer() as first_checkpointer:
            first_graph = _approval_interrupt_graph(
                checkpointer=first_checkpointer,
                approval_wait=ApprovalWaitService(database_session),
            )
            interrupted = first_graph.invoke(state, config)

        with open_postgres_checkpointer() as second_checkpointer:
            second_graph = _approval_interrupt_graph(
                checkpointer=second_checkpointer,
                approval_wait=ApprovalWaitService(database_session),
            )
            recovered = second_graph.get_state(config).values
            reader_recovered = PostgresWorkflowStateReader().get_state(incident.thread_id)

        database_session.refresh(incident)
        assert "__interrupt__" in interrupted
        interrupt_value = interrupted["__interrupt__"][0].value
        assert interrupt_value["action_id"] == str(action.id)
        assert "approval_id" not in interrupt_value
        assert "approved" not in interrupt_value
        assert recovered["current_stage"] == AgentStage.WAITING_APPROVAL
        assert recovered["policy_outcome"] is not None
        assert recovered["policy_outcome"].action_id == action.id
        assert reader_recovered["current_stage"] == AgentStage.WAITING_APPROVAL
        assert incident.status == WAITING_APPROVAL
        assert database_session.query(Approval).count() == 0
    finally:
        _delete_thread(incident.thread_id)


def test_approval_service_rejects_checkpoint_state_without_waiting_stage(
    database_session: Session,
) -> None:
    incident = _incident(database_session)
    action = _pending_action(database_session, incident)
    state = _waiting_state(incident, action)
    state["current_stage"] = AgentStage.POLICY_GATE

    with pytest.raises(ApprovalValidationError, match="not waiting"):
        ApprovalService(database_session, StaticWorkflowStateReader(state)).record_decision(
            incident.id, ApprovalDecision.APPROVE
        )
    assert database_session.query(Approval).count() == 0
