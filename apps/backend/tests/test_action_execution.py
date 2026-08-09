"""Task 4.4 tests for the database-bound controlled rollback boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.orm import Session

from devsupport_backend.action_execution import (
    ActionExecutionService,
    controlled_action_execution_node,
)
from devsupport_backend.agent.state import (
    ActionType,
    AgentStage,
    ApprovalOutcome,
    ApprovalStatus,
    PolicyDecision,
    PolicyOutcome,
    PolicyReasonCode,
    create_initial_agent_state,
)
from devsupport_backend.models import Action, Approval, Incident
from devsupport_backend.tools.deployments import DeploymentQueryResult, RollbackResult
from devsupport_backend.tools.schemas import ToolStatus


class DeploymentAdapter:
    def __init__(self, deployment: DeploymentQueryResult) -> None:
        self.deployment = deployment

    def query(self, _tool_input):
        return self.deployment


class RollbackAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, tool_input):
        self.calls += 1
        return RollbackResult(
            service=tool_input.service,
            environment=tool_input.environment,
            target_version=tool_input.target_version,
            executed=True,
        )


def _approved_state(session: Session):
    now = datetime.now(UTC)
    incident = Incident(
        id=uuid4(),
        service="order-service",
        environment="local",
        status="REMEDIATING",
        description="Controlled rollback test.",
        time_range_start=now,
        time_range_end=now + timedelta(minutes=1),
        thread_id=str(uuid4()),
    )
    session.add(incident)
    session.commit()
    action = Action(
        incident_id=incident.id,
        action_type=ActionType.ROLLBACK_DEPLOYMENT.value,
        status="APPROVED",
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
    approval = Approval(incident_id=incident.id, action_id=action.id, status="APPROVED")
    session.add(approval)
    session.commit()
    state = create_initial_agent_state(incident)
    state.update(
        {
            "current_stage": AgentStage.ACTION_EXECUTION,
            "policy_outcome": PolicyOutcome(
                decision=PolicyDecision.APPROVAL_REQUIRED,
                reason_code=PolicyReasonCode.APPROVAL_REQUIRED,
                reason="Approved action is ready.",
                action_id=action.id,
            ),
            "approval_outcome": ApprovalOutcome(
                approval_id=approval.id,
                action_id=action.id,
                status=ApprovalStatus.APPROVED,
            ),
        }
    )
    return incident, action, approval, state


def test_approved_action_executes_only_after_live_deployment_matches(
    database_session: Session,
) -> None:
    incident, action, approval, state = _approved_state(database_session)
    rollback = RollbackAdapter()
    service = ActionExecutionService(
        database_session,
        DeploymentAdapter(DeploymentQueryResult("order-service", "v1.1.0", "v1.0.0", None)),
        rollback,
    )

    updated = controlled_action_execution_node(state, service)

    database_session.refresh(action)
    database_session.refresh(incident)
    assert rollback.calls == 1
    assert action.status == "EXECUTED"
    assert action.executed_at is not None
    assert incident.status == "VERIFYING"
    assert updated["current_stage"] is AgentStage.RECOVERY_VERIFICATION
    assert updated["execution_outcome"] is not None
    assert updated["execution_outcome"].action_id == action.id
    assert updated["execution_outcome"].approval_id == approval.id
    assert updated["execution_outcome"].target_version == "v1.0.0"
    assert updated["execution_outcome"].status is ToolStatus.SUCCESS


def test_stale_live_deployment_fails_closed_without_a_rollback_call(
    database_session: Session,
) -> None:
    incident, action, _, state = _approved_state(database_session)
    rollback = RollbackAdapter()
    service = ActionExecutionService(
        database_session,
        DeploymentAdapter(DeploymentQueryResult("order-service", "v1.2.0", "v1.1.0", None)),
        rollback,
    )

    updated = controlled_action_execution_node(state, service)

    database_session.refresh(action)
    database_session.refresh(incident)
    assert rollback.calls == 0
    assert action.status == "FAILED"
    assert action.executed_at is None
    assert incident.status == "NEEDS_MANUAL_ACTION"
    assert updated["current_stage"] is AgentStage.NEEDS_MANUAL_ACTION


def test_binding_failure_does_not_invent_action_or_approval_ids(database_session: Session) -> None:
    incident, _, _, state = _approved_state(database_session)
    state["policy_outcome"] = None
    rollback = RollbackAdapter()
    service = ActionExecutionService(
        database_session,
        DeploymentAdapter(DeploymentQueryResult("order-service", "v1.1.0", "v1.0.0", None)),
        rollback,
    )

    updated = controlled_action_execution_node(state, service)

    database_session.refresh(incident)
    outcome = updated["execution_outcome"]
    assert rollback.calls == 0
    assert outcome is not None
    assert outcome.status is ToolStatus.FAILURE
    assert outcome.action_id is None
    assert outcome.approval_id is None
    assert outcome.service is None
    assert outcome.environment is None
    assert outcome.target_version is None
    assert outcome.executed is False
    assert incident.status == "NEEDS_MANUAL_ACTION"


def test_invalid_action_parameters_do_not_invent_a_target_version(
    database_session: Session,
) -> None:
    _, action, approval, state = _approved_state(database_session)
    action.parameters = {"service": "order-service"}
    database_session.commit()
    rollback = RollbackAdapter()
    service = ActionExecutionService(
        database_session,
        DeploymentAdapter(DeploymentQueryResult("order-service", "v1.1.0", "v1.0.0", None)),
        rollback,
    )

    updated = controlled_action_execution_node(state, service)

    outcome = updated["execution_outcome"]
    assert rollback.calls == 0
    assert outcome is not None
    assert outcome.status is ToolStatus.FAILURE
    assert outcome.action_id == action.id
    assert outcome.approval_id == approval.id
    assert outcome.target_version is None
    assert outcome.executed is False


def test_crash_retry_reconciles_existing_live_rollback_without_second_call(
    database_session: Session,
) -> None:
    incident, action, _, state = _approved_state(database_session)
    rollback = RollbackAdapter()
    service = ActionExecutionService(
        database_session,
        DeploymentAdapter(DeploymentQueryResult("order-service", "v1.0.0", "v1.1.0", None)),
        rollback,
    )

    updated = controlled_action_execution_node(state, service)

    database_session.refresh(action)
    database_session.refresh(incident)
    assert rollback.calls == 0
    assert action.status == "EXECUTED"
    assert incident.status == "VERIFYING"
    assert updated["execution_outcome"] is not None
    assert updated["execution_outcome"].executed is False
