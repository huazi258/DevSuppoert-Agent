"""Task 4.5 deterministic recovery verification tests."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from devsupport_backend.agent.state import (
    ActionExecutionOutcome,
    ActionType,
    AgentStage,
    ApprovalOutcome,
    ApprovalStatus,
    PolicyDecision,
    PolicyOutcome,
    PolicyReasonCode,
    TerminalReason,
    VerificationOutcome,
    VerificationStatus,
    create_initial_agent_state,
)
from devsupport_backend.models import Action, Approval, Incident, Verification
from devsupport_backend.recovery_verification import (
    RecoveryVerificationService,
    recovery_verification_node,
)
from devsupport_backend.tools.adapter_contracts import (
    DeploymentQueryResult,
    LogQueryResult,
    MetricsQueryResult,
)
from devsupport_backend.tools.deployments import DeploymentAdapterError
from devsupport_backend.tools.logs import LogsAdapterError
from devsupport_backend.tools.metrics import MetricsAdapterError
from devsupport_backend.tools.recovery_probe import RecoveryProbeResult


class DeploymentAdapter:
    def __init__(self, current: str = "v1.0.0") -> None:
        self.current = current

    def query(self, _input):
        return DeploymentQueryResult("order-service", self.current, "v1.1.0", None)


class MetricsAdapter:
    def __init__(self, *, health: str = "ok", error_delta: int = 0) -> None:
        self.calls = 0
        self.health = health
        self.error_delta = error_delta

    def query(self, _input):
        self.calls += 1
        return MetricsQueryResult(
            "order-service",
            self.health,
            10 + self.calls,
            5 + self.calls,
            5 + (self.error_delta if self.calls > 1 else 0),
            None,
            None,
        )


class LogsAdapter:
    def __init__(self, count: int = 0) -> None:
        self.count = count

    def query(self, _input):
        return LogQueryResult(self.count, ())


class ProbeAdapter:
    def __init__(self, outcome: str = "pass") -> None:
        self.calls = 0
        self.outcome = outcome

    def probe(self):
        self.calls += 1
        return RecoveryProbeResult(
            self.outcome, 200 if self.outcome == "pass" else 502, "confirmed"
        )


def _state(session: Session):
    now = datetime.now(UTC)
    incident = Incident(
        id=uuid4(),
        service="order-service",
        environment="local",
        status="VERIFYING",
        description="Recovery verification test.",
        time_range_start=now,
        time_range_end=now + timedelta(minutes=1),
        thread_id=str(uuid4()),
    )
    session.add(incident)
    session.commit()
    action = Action(
        incident_id=incident.id,
        action_type=ActionType.ROLLBACK_DEPLOYMENT.value,
        status="EXECUTED",
        executed_at=now,
        parameters={
            "service": "order-service",
            "environment": "local",
            "current_version": "v1.1.0",
            "target_version": "v1.0.0",
            "reason": "Approved rollback.",
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
            "current_stage": AgentStage.RECOVERY_VERIFICATION,
            "policy_outcome": PolicyOutcome(
                decision=PolicyDecision.APPROVAL_REQUIRED,
                reason_code=PolicyReasonCode.APPROVAL_REQUIRED,
                reason="Approved.",
                action_id=action.id,
            ),
            "approval_outcome": ApprovalOutcome(
                approval_id=approval.id, action_id=action.id, status=ApprovalStatus.APPROVED
            ),
            "execution_outcome": ActionExecutionOutcome(
                action_id=action.id,
                approval_id=approval.id,
                status="success",
                service="order-service",
                environment="local",
                target_version="v1.0.0",
                executed=True,
            ),
        }
    )
    return incident, action, state


def test_pass_persists_one_verification_and_resolves(database_session: Session) -> None:
    incident, action, state = _state(database_session)
    probe = ProbeAdapter()
    service = RecoveryVerificationService(
        database_session, DeploymentAdapter(), MetricsAdapter(), LogsAdapter(), probe
    )

    first = service.verify(state)
    second = service.verify(state)

    database_session.refresh(incident)
    assert first.status is VerificationStatus.PASS
    assert second.verification_id == first.verification_id
    assert probe.calls == 1
    assert incident.status == "RESOLVED"
    assert (
        database_session.query(Verification).filter(Verification.action_id == action.id).count()
        == 1
    )


def test_failed_probe_needs_manual_action_and_keeps_action_executed(
    database_session: Session,
) -> None:
    incident, action, state = _state(database_session)
    service = RecoveryVerificationService(
        database_session, DeploymentAdapter(), MetricsAdapter(), LogsAdapter(), ProbeAdapter("fail")
    )

    outcome = service.verify(state)

    database_session.refresh(incident)
    database_session.refresh(action)
    assert outcome.status is VerificationStatus.FAIL
    assert incident.status == "NEEDS_MANUAL_ACTION"
    assert action.status == "EXECUTED"


def test_binding_failure_is_inconclusive_without_resolving(database_session: Session) -> None:
    incident, action, state = _state(database_session)
    state["execution_outcome"] = None
    service = RecoveryVerificationService(
        database_session, DeploymentAdapter(), MetricsAdapter(), LogsAdapter(), ProbeAdapter()
    )

    outcome = service.verify(state)

    database_session.refresh(incident)
    assert outcome.status is VerificationStatus.INCONCLUSIVE
    assert incident.status == "NEEDS_MANUAL_ACTION"


class RaisingDeploymentAdapter:
    def query(self, _input):
        raise DeploymentAdapterError("unavailable", "unavailable")


class RaisingMetricsAdapter:
    def __init__(self, after: bool = False) -> None:
        self.calls = 0
        self.after = after

    def query(self, _input):
        self.calls += 1
        if not self.after or self.calls > 1:
            raise MetricsAdapterError("unavailable", "unavailable")
        return MetricsQueryResult("order-service", "ok", 10, 5, 5, None, None)


class RaisingLogsAdapter:
    def query(self, _input):
        raise LogsAdapterError("unavailable", "unavailable")


@pytest.mark.parametrize(
    ("deployment", "metrics", "logs", "probe", "expected"),
    [
        (
            DeploymentAdapter("v9"),
            MetricsAdapter(),
            LogsAdapter(),
            ProbeAdapter(),
            VerificationStatus.FAIL,
        ),
        (
            DeploymentAdapter(),
            MetricsAdapter(health="down"),
            LogsAdapter(),
            ProbeAdapter(),
            VerificationStatus.FAIL,
        ),
        (
            DeploymentAdapter(),
            MetricsAdapter(),
            LogsAdapter(),
            ProbeAdapter("fail"),
            VerificationStatus.FAIL,
        ),
        (
            DeploymentAdapter(),
            MetricsAdapter(error_delta=1),
            LogsAdapter(),
            ProbeAdapter(),
            VerificationStatus.FAIL,
        ),
        (
            DeploymentAdapter(),
            MetricsAdapter(),
            LogsAdapter(1),
            ProbeAdapter(),
            VerificationStatus.FAIL,
        ),
        (
            RaisingDeploymentAdapter(),
            MetricsAdapter(),
            LogsAdapter(),
            ProbeAdapter(),
            VerificationStatus.INCONCLUSIVE,
        ),
        (
            DeploymentAdapter(),
            RaisingMetricsAdapter(),
            LogsAdapter(),
            ProbeAdapter(),
            VerificationStatus.INCONCLUSIVE,
        ),
        (
            DeploymentAdapter(),
            RaisingMetricsAdapter(after=True),
            LogsAdapter(),
            ProbeAdapter(),
            VerificationStatus.INCONCLUSIVE,
        ),
        (
            DeploymentAdapter(),
            MetricsAdapter(),
            RaisingLogsAdapter(),
            ProbeAdapter(),
            VerificationStatus.INCONCLUSIVE,
        ),
    ],
)
def test_deterministic_fail_and_inconclusive_matrix(
    database_session: Session, deployment, metrics, logs, probe, expected: VerificationStatus
) -> None:
    incident, action, state = _state(database_session)
    outcome = RecoveryVerificationService(
        database_session, deployment, metrics, logs, probe
    ).verify(state)
    database_session.refresh(incident)
    database_session.refresh(action)
    assert outcome.status is expected
    assert incident.status == "NEEDS_MANUAL_ACTION"
    assert action.status == "EXECUTED"
    verification = (
        database_session.query(Verification).filter(Verification.action_id == action.id).one()
    )
    assert verification.details["verification_started_at"]
    assert verification.details["verification_completed_at"]
    assert outcome.action_id == action.id
    assert (
        database_session.query(Verification).filter(Verification.action_id == action.id).count()
        == 1
    )


@pytest.mark.parametrize(
    ("status", "stage", "terminal_reason"),
    [
        (VerificationStatus.PASS, AgentStage.RESOLVED, None),
        (
            VerificationStatus.FAIL,
            AgentStage.NEEDS_MANUAL_ACTION,
            TerminalReason.RECOVERY_VERIFICATION_FAILED,
        ),
        (
            VerificationStatus.INCONCLUSIVE,
            AgentStage.NEEDS_MANUAL_ACTION,
            TerminalReason.RECOVERY_VERIFICATION_INCONCLUSIVE,
        ),
    ],
)
def test_recovery_verification_node_projects_terminal_reasons(
    database_session: Session,
    status: VerificationStatus,
    stage: AgentStage,
    terminal_reason: TerminalReason | None,
) -> None:
    _, _, state = _state(database_session)

    class StaticVerificationService:
        def verify(self, current: object) -> VerificationOutcome:
            del current
            return VerificationOutcome(status=status, summary="Controlled verification outcome.")

    updated = recovery_verification_node(state, StaticVerificationService())

    assert updated["current_stage"] is stage
    assert updated["terminal_reason"] is terminal_reason
