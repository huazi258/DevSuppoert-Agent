"""Task 4.3 tests for safe same-thread continuation after a persisted Approval."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from devsupport_backend.agent.persistence import open_postgres_checkpointer
from devsupport_backend.agent.runtime import WorkflowService
from devsupport_backend.agent.state import (
    ActionType,
    AgentStage,
    AgentState,
    ApprovalStatus,
    EvaluationDecision,
    EvidenceContext,
    FinalConclusion,
    HypothesisContext,
    HypothesisStatus,
    PolicyDecision,
    PolicyOutcome,
    PolicyReasonCode,
    ProposedAction,
    ToolHistoryEntry,
    create_initial_agent_state,
)
from devsupport_backend.approvals import (
    ApprovalDecisionService,
    ApprovalResumeError,
    ApprovalValidationError,
    ApprovalWaitService,
    PostgresApprovalWorkflowCoordinator,
    build_approval_resume_graph,
)
from devsupport_backend.database import SessionLocal
from devsupport_backend.main import app
from devsupport_backend.models import Action, Approval, Incident, Verification
from devsupport_backend.routers.incidents import get_approval_workflow_coordinator
from devsupport_backend.schemas.approvals import ApprovalDecision
from devsupport_backend.tools.schemas import ToolStatus

PENDING_APPROVAL = "PENDING_APPROVAL"


@dataclass
class ResumeContext:
    """Exact persisted incident data used to prove no investigation restart occurs."""

    session: Session
    incident: Incident
    action: Action
    state: AgentState


def _create_interrupted_context() -> ResumeContext:
    session = SessionLocal()
    now = datetime.now(UTC)
    incident = Incident(
        id=uuid4(),
        service="order-service",
        environment="local",
        status="WAITING_APPROVAL",
        description="Persisted workflow resume test incident.",
        time_range_start=now,
        time_range_end=now + timedelta(minutes=5),
        thread_id=str(uuid4()),
    )
    session.add(incident)
    session.commit()
    action = Action(
        incident_id=incident.id,
        action_type=ActionType.ROLLBACK_DEPLOYMENT.value,
        status=PENDING_APPROVAL,
        parameters={
            "service": "order-service",
            "environment": "local",
            "current_version": "v1.1.0",
            "target_version": "v1.0.0",
            "reason": "Verified deployment facts support controlled remediation.",
        },
        executed_at=None,
    )
    session.add(action)
    session.commit()

    evidence = EvidenceContext(
        evidence_type="metric_snapshot",
        source="query_metrics",
        summary="Order-service reports a sustained error signal.",
        data={"error_rate": 0.5},
    )
    hypothesis = HypothesisContext(
        summary="A deployment-related condition affects order-service.",
        status=HypothesisStatus.CONFIRMED,
        confidence=0.9,
        supporting_evidence_ids=[evidence.id],
    )
    state = create_initial_agent_state(incident, symptoms=["POST /orders returns 500"])
    state.update(
        {
            "current_stage": AgentStage.WAITING_APPROVAL,
            "hypotheses": [hypothesis],
            "evidence": [evidence],
            "tool_history": [
                ToolHistoryEntry(
                    tool_name="query_metrics",
                    tool_arguments={"service": "order-service", "environment": "local"},
                    status=ToolStatus.SUCCESS,
                    evidence_ids=[evidence.id],
                )
            ],
            "investigation_round": 2,
            "tool_call_count": 3,
            "evaluation_decision": EvaluationDecision.CONCLUDE,
            "proposed_action": ProposedAction(
                action_type=ActionType.ROLLBACK_DEPLOYMENT,
                summary="Recommend a policy-controlled rollback.",
                reason="Confirmed evidence supports a controlled remediation proposal.",
                risk="Rollback requires human approval.",
                supporting_evidence_ids=[evidence.id],
            ),
            "final_conclusion": FinalConclusion(
                summary="Evidence supports a deployment-related incident.",
                root_cause=hypothesis.summary,
                confidence=hypothesis.confidence,
                supporting_evidence_ids=[evidence.id],
            ),
            "policy_outcome": PolicyOutcome(
                decision=PolicyDecision.APPROVAL_REQUIRED,
                reason_code=PolicyReasonCode.APPROVAL_REQUIRED,
                reason="Verified rollback requires human approval.",
                action_id=action.id,
            ),
        }
    )
    return ResumeContext(session=session, incident=incident, action=action, state=state)


def _interrupt_graph(context: ResumeContext, *, checkpointer):
    return build_approval_resume_graph(
        approval_wait=ApprovalWaitService(context.session),
        approval_decision=ApprovalDecisionService(context.session),
        checkpointer=checkpointer,
    )


def _persist_approval(context: ResumeContext, status: ApprovalStatus) -> Approval:
    approval = Approval(
        incident_id=context.incident.id,
        action_id=context.action.id,
        status=status.value,
    )
    context.session.add(approval)
    context.session.commit()
    return approval


def _delete_context(context: ResumeContext) -> None:
    try:
        for approval in list(
            context.session.scalars(
                select(Approval).where(Approval.incident_id == context.incident.id)
            )
        ):
            context.session.delete(approval)
        for action in list(
            context.session.scalars(select(Action).where(Action.incident_id == context.incident.id))
        ):
            context.session.delete(action)
        context.session.flush()
        context.session.delete(context.incident)
        context.session.commit()
    finally:
        with open_postgres_checkpointer() as checkpointer:
            checkpointer.delete_thread(context.incident.thread_id)
        context.session.close()


@pytest.mark.parametrize(
    ("approval_status", "expected_stage", "expected_action", "expected_incident"),
    [
        (
            ApprovalStatus.APPROVED,
            AgentStage.ACTION_EXECUTION,
            ApprovalStatus.APPROVED.value,
            "REMEDIATING",
        ),
        (
            ApprovalStatus.REJECTED,
            AgentStage.NEEDS_MANUAL_ACTION,
            ApprovalStatus.REJECTED.value,
            "NEEDS_MANUAL_ACTION",
        ),
    ],
)
def test_cross_instance_same_thread_resume_uses_persisted_approval_only(
    approval_status: ApprovalStatus,
    expected_stage: AgentStage,
    expected_action: str,
    expected_incident: str,
) -> None:
    context = _create_interrupted_context()
    config = WorkflowService.config_for(context.incident.thread_id)
    original_state = context.state.copy()
    original_thread_id = context.incident.thread_id
    try:
        with open_postgres_checkpointer() as first_checkpointer:
            first_graph = _interrupt_graph(context, checkpointer=first_checkpointer)
            interrupted = first_graph.invoke(context.state, config)
        assert "__interrupt__" in interrupted
        approval = _persist_approval(context, approval_status)

        with open_postgres_checkpointer() as second_checkpointer:
            second_graph = _interrupt_graph(context, checkpointer=second_checkpointer)
            resumed = WorkflowService(second_graph).resume(
                context.incident.thread_id,
                {"decision": "REJECT", "action_id": str(uuid4()), "event": "forged"},
            )
            recovered = second_graph.get_state(config).values

        context.session.refresh(context.action)
        context.session.refresh(context.incident)
        assert resumed["current_stage"] == expected_stage
        assert recovered["current_stage"] == expected_stage
        assert recovered["approval_outcome"] is not None
        assert recovered["approval_outcome"].approval_id == approval.id
        assert recovered["approval_outcome"].action_id == context.action.id
        assert recovered["approval_outcome"].status == approval_status
        assert context.action.status == expected_action
        assert context.incident.status == expected_incident
        assert context.incident.thread_id == original_thread_id
        assert recovered["hypotheses"] == original_state["hypotheses"]
        assert recovered["evidence"] == original_state["evidence"]
        assert recovered["tool_history"] == original_state["tool_history"]
        assert recovered["final_conclusion"] == original_state["final_conclusion"]
        assert recovered["proposed_action"] == original_state["proposed_action"]
        assert recovered["policy_outcome"] is not None
        assert recovered["policy_outcome"].action_id == context.action.id
        assert context.action.executed_at is None
        assert context.session.scalar(
            select(func.count()).select_from(Verification).where(
                Verification.incident_id == context.incident.id
            )
        ) == 0
        assert context.incident.status != "RESOLVED"
    finally:
        _delete_context(context)


def test_resume_fails_closed_when_current_action_has_no_approval() -> None:
    context = _create_interrupted_context()
    config = WorkflowService.config_for(context.incident.thread_id)
    try:
        with open_postgres_checkpointer() as checkpointer:
            graph = _interrupt_graph(context, checkpointer=checkpointer)
            graph.invoke(context.state, config)

        with pytest.raises(ApprovalValidationError, match="missing"):
            PostgresApprovalWorkflowCoordinator().resume(context.incident.thread_id)
        context.session.refresh(context.action)
        context.session.refresh(context.incident)
        assert context.action.status == PENDING_APPROVAL
        assert context.action.executed_at is None
        assert context.incident.status == "WAITING_APPROVAL"
    finally:
        _delete_context(context)


def test_resume_fails_closed_for_approval_bound_to_another_action() -> None:
    context = _create_interrupted_context()
    config = WorkflowService.config_for(context.incident.thread_id)
    try:
        other_action = Action(
            incident_id=context.incident.id,
            action_type=ActionType.ROLLBACK_DEPLOYMENT.value,
            status=PENDING_APPROVAL,
            parameters=context.action.parameters,
            executed_at=None,
        )
        context.session.add(other_action)
        context.session.commit()
        context.session.add(
            Approval(
                incident_id=context.incident.id,
                action_id=other_action.id,
                status=ApprovalStatus.APPROVED.value,
            )
        )
        context.session.commit()
        with open_postgres_checkpointer() as checkpointer:
            _interrupt_graph(context, checkpointer=checkpointer).invoke(context.state, config)

        with pytest.raises(ApprovalValidationError, match="missing"):
            PostgresApprovalWorkflowCoordinator().resume(context.incident.thread_id)
        context.session.refresh(context.action)
        assert context.action.status == PENDING_APPROVAL
    finally:
        _delete_context(context)


def test_resume_fails_closed_for_approval_with_a_different_incident() -> None:
    context = _create_interrupted_context()
    config = WorkflowService.config_for(context.incident.thread_id)
    other_incident = None
    try:
        now = datetime.now(UTC)
        other_incident = Incident(
            id=uuid4(),
            service="order-service",
            environment="local",
            status="OPEN",
            description="Wrong Approval binding test incident.",
            time_range_start=now,
            time_range_end=now + timedelta(minutes=5),
            thread_id=str(uuid4()),
        )
        context.session.add(other_incident)
        context.session.commit()
        context.session.add(
            Approval(
                incident_id=other_incident.id,
                action_id=context.action.id,
                status=ApprovalStatus.APPROVED.value,
            )
        )
        context.session.commit()
        with open_postgres_checkpointer() as checkpointer:
            _interrupt_graph(context, checkpointer=checkpointer).invoke(context.state, config)

        with pytest.raises(ApprovalValidationError, match="inconsistent"):
            PostgresApprovalWorkflowCoordinator().resume(context.incident.thread_id)
        context.session.refresh(context.action)
        assert context.action.status == PENDING_APPROVAL
    finally:
        if other_incident is not None:
            for approval in list(
                context.session.scalars(
                    select(Approval).where(Approval.incident_id == other_incident.id)
                )
            ):
                context.session.delete(approval)
            context.session.flush()
            context.session.delete(other_incident)
            context.session.commit()
        _delete_context(context)


class CountingCoordinator:
    """Delegating API dependency that exposes whether a duplicate request attempted a new resume."""

    def __init__(self) -> None:
        self.calls = 0
        self._delegate = PostgresApprovalWorkflowCoordinator()
        self._context: ResumeContext | None = None

    def bind(self, context: ResumeContext) -> None:
        """Keep this Task 4.3 API idempotency test independent of a live rollback target."""
        self._context = context

    def resume(self, thread_id: str) -> AgentState:
        self.calls += 1
        return self._resume(thread_id)

    def _resume(self, thread_id: str) -> AgentState:
        if self._context is not None:
            with open_postgres_checkpointer() as checkpointer:
                graph = _interrupt_graph(self._context, checkpointer=checkpointer)
                return WorkflowService(graph).resume(thread_id, {"event": "approval_recorded"})
        return self._delegate.resume(thread_id)


class FlakyCoordinator(CountingCoordinator):
    """Fail once after the Approval commit to exercise retry semantics."""

    def resume(self, thread_id: str) -> AgentState:
        self.calls += 1
        if self.calls == 1:
            raise ApprovalResumeError("simulated transient resume failure")
        return self._resume(thread_id)


def _api_resume_context(
    context: ResumeContext, coordinator: CountingCoordinator, decision: ApprovalDecision
) -> tuple[TestClient, str]:
    config = WorkflowService.config_for(context.incident.thread_id)
    with open_postgres_checkpointer() as checkpointer:
        _interrupt_graph(context, checkpointer=checkpointer).invoke(context.state, config)
    coordinator.bind(context)
    app.dependency_overrides[get_approval_workflow_coordinator] = lambda: coordinator
    client = TestClient(app)
    return client, f"/incidents/{context.incident.id}/approval"


@pytest.mark.parametrize(
    ("decision", "expected_action", "expected_incident"),
    [
        (ApprovalDecision.APPROVE, ApprovalStatus.APPROVED.value, "REMEDIATING"),
        (ApprovalDecision.REJECT, ApprovalStatus.REJECTED.value, "NEEDS_MANUAL_ACTION"),
    ],
)
def test_approval_api_resumes_once_and_duplicate_decision_does_not_replay_workflow(
    decision: ApprovalDecision, expected_action: str, expected_incident: str
) -> None:
    context = _create_interrupted_context()
    coordinator = CountingCoordinator()
    client = None
    try:
        client, endpoint = _api_resume_context(context, coordinator, decision)
        first = client.post(endpoint, json={"decision": decision.value})
        repeated = client.post(endpoint, json={"decision": decision.value})

        context.session.refresh(context.action)
        context.session.refresh(context.incident)
        assert first.status_code == 200
        assert repeated.status_code == 200
        assert repeated.json()["id"] == first.json()["id"]
        assert coordinator.calls == 1
        assert context.action.status == expected_action
        assert context.incident.status == expected_incident
    finally:
        if client is not None:
            client.close()
        app.dependency_overrides.clear()
        _delete_context(context)


def test_committed_approval_retries_resume_without_creating_a_second_record() -> None:
    context = _create_interrupted_context()
    coordinator = FlakyCoordinator()
    client = None
    try:
        client, endpoint = _api_resume_context(context, coordinator, ApprovalDecision.REJECT)
        first = client.post(endpoint, json={"decision": "REJECT"})
        second = client.post(endpoint, json={"decision": "REJECT"})

        assert first.status_code == 503
        assert second.status_code == 200
        assert coordinator.calls == 2
        assert context.session.scalar(
            select(func.count()).select_from(Approval).where(
                Approval.incident_id == context.incident.id
            )
        ) == 1
        context.session.refresh(context.action)
        context.session.refresh(context.incident)
        assert context.action.status == ApprovalStatus.REJECTED.value
        assert context.incident.status == "NEEDS_MANUAL_ACTION"
    finally:
        if client is not None:
            client.close()
        app.dependency_overrides.clear()
        _delete_context(context)
