"""Web workflow projection and lifecycle tests without external providers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from devsupport_backend.agent.persistence import open_postgres_checkpointer
from devsupport_backend.agent.state import (
    ActionType,
    AgentStage,
    AgentState,
    EvidenceContext,
    FinalConclusion,
    HypothesisContext,
    HypothesisStatus,
    PolicyDecision,
    PolicyOutcome,
    PolicyReasonCode,
    ProposedAction,
    create_initial_agent_state,
)
from devsupport_backend.models import Action, Incident
from devsupport_backend.workflow_console import (
    PostgresWorkflowRuntime,
    WorkflowConflictError,
    WorkflowConsoleService,
    WorkflowStartError,
    WorkflowStateConflict,
    project_workflow_response,
)


class FakeRuntime:
    def __init__(
        self,
        *,
        state=None,
        states: list[object | None] | None = None,
        start_error: Exception | None = None,
    ) -> None:
        self.state = state
        self.states = states or []
        self.start_error = start_error
        self.start_calls = 0
        self.thread_ids: list[str] = []

    def get_state(self, thread_id: str):
        self.thread_ids.append(thread_id)
        if self.states:
            state = self.states.pop(0)
            if isinstance(state, Exception):
                raise state
            return state
        return self.state

    def start(self, incident: Incident):
        self.start_calls += 1
        self.thread_ids.append(incident.thread_id)
        if self.start_error:
            raise self.start_error
        if self.state is None:
            raise AssertionError("successful fake start requires a state")
        return self.state


def _incident(session: Session, *, status: str = "OPEN") -> Incident:
    now = datetime.now(UTC)
    incident = Incident(
        service="order-service",
        environment="local",
        status=status,
        description="Workflow console test incident.",
        time_range_start=now,
        time_range_end=now + timedelta(minutes=5),
        thread_id=str(uuid4()),
    )
    session.add(incident)
    session.commit()
    return incident


def _state(incident: Incident, action: Action | None = None):
    evidence = EvidenceContext(
        evidence_type="metric_snapshot",
        source="query_metrics",
        summary="Current error metrics are available.",
        data={"secret_internal_payload": "must-not-be-exposed"},
    )
    hypothesis = HypothesisContext(
        summary="A deployment configuration is suspect.",
        status=HypothesisStatus.CONFIRMED,
        confidence=0.9,
        supporting_evidence_ids=[evidence.id],
    )
    state = create_initial_agent_state(incident)
    state.update(
        {
            "current_stage": AgentStage.EVIDENCE_EVALUATION,
            "hypotheses": [hypothesis],
            "evidence": [evidence],
            "final_conclusion": FinalConclusion(
                summary="Evidence confirms a deployment issue.",
                root_cause=hypothesis.summary,
                confidence=0.9,
                supporting_evidence_ids=[evidence.id],
            ),
            "proposed_action": ProposedAction(
                action_type=ActionType.ROLLBACK_DEPLOYMENT,
                summary="Recommend rollback.",
                parameters={"target_version": "untrusted"},
                reason="Evidence supports a controlled action.",
                risk="Requires approval.",
                supporting_evidence_ids=[evidence.id],
            ),
            "policy_outcome": (
                PolicyOutcome(
                    decision=PolicyDecision.APPROVAL_REQUIRED,
                    reason_code=PolicyReasonCode.APPROVAL_REQUIRED,
                    reason="Verified action requires approval.",
                    action_id=action.id,
                )
                if action
                else None
            ),
        }
    )
    return state


def _action(session: Session, incident: Incident) -> Action:
    action = Action(
        incident_id=incident.id,
        action_type="rollback_deployment",
        status="PENDING_APPROVAL",
        parameters={
            "service": "order-service",
            "environment": "local",
            "current_version": "v1.1.0",
            "target_version": "v1.0.0",
            "reason": "Verified deployment facts require rollback.",
        },
    )
    session.add(action)
    session.commit()
    return action


def test_projector_exposes_only_bound_public_facts(database_session: Session) -> None:
    incident = _incident(database_session)
    action = _action(database_session, incident)

    response = project_workflow_response(incident, _state(incident, action), action)

    body = response.model_dump(mode="json")
    assert body["incident_id"] == str(incident.id)
    assert body["evidence"][0].get("data") is None
    assert "data" not in body["evidence"][0]
    assert body["proposed_action"].get("parameters") is None
    assert body["action"]["parameters"] == {
        "service": "order-service",
        "environment": "local",
        "current_version": "v1.1.0",
        "target_version": "v1.0.0",
        "reason": "Verified deployment facts require rollback.",
    }


def test_projector_rejects_action_and_incident_binding_mismatches(
    database_session: Session,
) -> None:
    incident = _incident(database_session)
    action = _action(database_session, incident)
    other_incident = _incident(database_session)
    other_action = _action(database_session, other_incident)
    state = _state(incident, action)

    with pytest.raises(WorkflowStateConflict, match="Action"):
        project_workflow_response(incident, state, other_action)

    mismatched = state.copy()
    mismatched["incident"] = state["incident"].model_copy(update={"service": "payment-service"})
    with pytest.raises(WorkflowStateConflict, match="Incident"):
        project_workflow_response(incident, mismatched, action)


def test_projector_rejects_invalid_persisted_action_parameters(
    database_session: Session,
) -> None:
    incident = _incident(database_session)
    action = _action(database_session, incident)
    action.parameters = {
        "service": "order-service",
        "environment": "local",
        "current_version": "v1.1.0",
        "reason": "Verified deployment facts require rollback.",
    }
    database_session.commit()

    with pytest.raises(
        WorkflowStateConflict,
        match="Persisted Action parameters are invalid",
    ):
        project_workflow_response(incident, _state(incident, action), action)


def test_start_reuses_thread_and_conflicts_on_existing_checkpoint(
    database_session: Session,
) -> None:
    incident = _incident(database_session)
    runtime = FakeRuntime(state=_state(incident), states=[None])
    service = WorkflowConsoleService(database_session, runtime)

    response = service.start(incident.id)

    database_session.refresh(incident)
    assert response.incident_id == incident.id
    assert incident.status == "INVESTIGATING"
    assert runtime.start_calls == 1
    assert runtime.thread_ids[-1] == incident.thread_id
    with pytest.raises(WorkflowConflictError):
        service.start(incident.id)
    assert runtime.start_calls == 1


def test_start_failure_restores_open_only_without_a_checkpoint(database_session: Session) -> None:
    incident = _incident(database_session)
    runtime = FakeRuntime(start_error=RuntimeError("provider unavailable"))

    with pytest.raises(WorkflowStartError):
        WorkflowConsoleService(database_session, runtime).start(incident.id)

    database_session.refresh(incident)
    assert incident.status == "OPEN"
    assert runtime.start_calls == 1


def test_start_failure_preserves_existing_checkpoint_status(database_session: Session) -> None:
    incident = _incident(database_session)
    runtime = FakeRuntime(
        states=[None, _state(incident)],
        start_error=RuntimeError("interrupted"),
    )
    service = WorkflowConsoleService(database_session, runtime)

    with pytest.raises(WorkflowStartError):
        service.start(incident.id)

    database_session.refresh(incident)
    assert runtime.start_calls == 1
    assert incident.status == "INVESTIGATING"


def test_start_reconciliation_read_failure_preserves_investigating(
    database_session: Session,
) -> None:
    incident = _incident(database_session)
    runtime = FakeRuntime(
        states=[None, RuntimeError("checkpoint unavailable")],
        start_error=RuntimeError("provider unavailable"),
    )

    with pytest.raises(WorkflowStartError):
        WorkflowConsoleService(database_session, runtime).start(incident.id)

    database_session.refresh(incident)
    assert runtime.start_calls == 1
    assert incident.status == "INVESTIGATING"


def test_postgres_runtime_reads_existing_checkpoint_without_writing(
    database_session: Session,
) -> None:
    incident = _incident(database_session)
    state = _state(incident)
    try:
        with open_postgres_checkpointer() as checkpointer:
            graph = StateGraph(AgentState)
            graph.add_node("checkpoint_writer", lambda current: current)
            graph.add_edge(START, "checkpoint_writer")
            graph.add_edge("checkpoint_writer", END)
            graph.compile(checkpointer=checkpointer).invoke(
                state, {"configurable": {"thread_id": incident.thread_id}}
            )

        recovered = PostgresWorkflowRuntime(database_session).get_state(incident.thread_id)

        assert recovered is not None
        assert recovered["incident"].id == incident.id
        assert recovered["evidence"][0].data == state["evidence"][0].data
    finally:
        with open_postgres_checkpointer() as checkpointer:
            checkpointer.delete_thread(incident.thread_id)
