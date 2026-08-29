"""Task 4.1 tests for code-level, non-executing rollback policy decisions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from devsupport_backend.agent.nodes.planner import READ_ONLY_INVESTIGATION_TOOLS as PLANNER_TOOLS
from devsupport_backend.agent.nodes.tool_execution import (
    READ_ONLY_INVESTIGATION_TOOLS as EXECUTOR_TOOLS,
)
from devsupport_backend.agent.policy import PENDING_APPROVAL, PolicyGateService, policy_gate_node
from devsupport_backend.agent.state import (
    ActionType,
    AgentStage,
    AgentState,
    EvaluationDecision,
    FinalConclusion,
    PolicyDecision,
    PolicyOutcome,
    PolicyReasonCode,
    ProposedAction,
    TerminalReason,
    create_initial_agent_state,
)
from devsupport_backend.models import Action, Incident
from devsupport_backend.tools.deployments import DeploymentAdapterError, DeploymentQueryResult
from devsupport_backend.tools.registry import ToolName
from devsupport_backend.tools.schemas import GetDeploymentHistoryInput


class RecordingDeploymentAdapter:
    """Return one controlled deployment snapshot while recording Policy Gate calls."""

    def __init__(self, result: DeploymentQueryResult | Exception) -> None:
        self._result = result
        self.calls: list[GetDeploymentHistoryInput] = []

    def query(self, tool_input: GetDeploymentHistoryInput) -> DeploymentQueryResult:
        self.calls.append(tool_input)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _deployment(
    *,
    service: str = "order-service",
    current_version: str = "v1.1.0",
    previous_version: str | None = "v1.0.0",
) -> DeploymentQueryResult:
    return DeploymentQueryResult(
        service=service,  # type: ignore[arg-type]
        current_version=current_version,
        previous_version=previous_version,
        deployed_at=None,
    )


def _persisted_incident(
    session: Session, *, service: str = "order-service", environment: str = "local"
) -> Incident:
    now = datetime.now(UTC)
    incident = Incident(
        id=uuid4(),
        service=service,
        environment=environment,
        description="A concluded incident for Policy Gate testing.",
        time_range_start=now,
        time_range_end=now + timedelta(minutes=5),
        status="OPEN",
        thread_id=str(uuid4()),
    )
    session.add(incident)
    session.commit()
    return incident


def _concluded_state(
    incident: Incident, *, action_type: ActionType = ActionType.ROLLBACK_DEPLOYMENT
) -> AgentState:
    state = create_initial_agent_state(incident)
    state["current_stage"] = AgentStage.CONCLUSION
    state["evaluation_decision"] = EvaluationDecision.CONCLUDE
    state["final_conclusion"] = FinalConclusion(
        summary="The evidence supports a deployment-related root cause.",
        root_cause="A deployment-related condition affects the incident service.",
    )
    state["proposed_action"] = ProposedAction(
        action_type=action_type,
        summary="Recommend operator review of a rollback.",
        reason="The confirmed evidence supports a controlled rollback proposal.",
        risk="Any operational change requires policy review and human approval.",
    )
    return state


def _actions_for(session: Session, incident: Incident) -> list[Action]:
    return list(session.scalars(select(Action).where(Action.incident_id == incident.id)))


def test_valid_local_rollback_prepares_one_precise_pending_action(
    database_session: Session,
) -> None:
    incident = _persisted_incident(database_session)
    adapter = RecordingDeploymentAdapter(_deployment())

    outcome = PolicyGateService(database_session, adapter).evaluate(_concluded_state(incident))  # type: ignore[arg-type]

    actions = _actions_for(database_session, incident)
    assert outcome.decision is PolicyDecision.APPROVAL_REQUIRED
    assert outcome.reason_code is PolicyReasonCode.APPROVAL_REQUIRED
    assert outcome.action_id == actions[0].id
    assert len(actions) == 1
    assert actions[0].action_type == ActionType.ROLLBACK_DEPLOYMENT.value
    assert actions[0].status == PENDING_APPROVAL
    assert actions[0].executed_at is None
    assert actions[0].parameters == {
        "service": "order-service",
        "environment": "local",
        "current_version": "v1.1.0",
        "target_version": "v1.0.0",
        "reason": "The confirmed evidence supports a controlled rollback proposal.",
    }
    assert adapter.calls == [
        GetDeploymentHistoryInput(service="order-service", environment="local")
    ]


@pytest.mark.parametrize(
    ("environment", "reason_code"),
    [
        ("production", PolicyReasonCode.PRODUCTION_ENVIRONMENT),
        ("staging", PolicyReasonCode.UNSUPPORTED_ENVIRONMENT),
    ],
)
def test_non_local_environments_are_denied_without_deployment_query(
    database_session: Session, environment: str, reason_code: PolicyReasonCode
) -> None:
    incident = _persisted_incident(database_session, environment=environment)
    adapter = RecordingDeploymentAdapter(_deployment())

    outcome = PolicyGateService(database_session, adapter).evaluate(_concluded_state(incident))  # type: ignore[arg-type]

    assert outcome.decision is PolicyDecision.DENIED
    assert outcome.reason_code is reason_code
    assert outcome.action_id is None
    assert adapter.calls == []
    assert _actions_for(database_session, incident) == []


def test_unsupported_incident_service_is_denied_without_deployment_query(
    database_session: Session,
) -> None:
    incident = _persisted_incident(database_session, service="catalog-service")
    adapter = RecordingDeploymentAdapter(_deployment(service="catalog-service"))

    outcome = PolicyGateService(database_session, adapter).evaluate(_concluded_state(incident))  # type: ignore[arg-type]

    assert outcome.decision is PolicyDecision.DENIED
    assert outcome.reason_code is PolicyReasonCode.UNSUPPORTED_SERVICE
    assert adapter.calls == []
    assert _actions_for(database_session, incident) == []


def test_manual_action_is_denied_without_creating_an_action(database_session: Session) -> None:
    incident = _persisted_incident(database_session)
    adapter = RecordingDeploymentAdapter(_deployment())

    outcome = PolicyGateService(database_session, adapter).evaluate(
        _concluded_state(incident, action_type=ActionType.MANUAL_ACTION)
    )  # type: ignore[arg-type]

    assert outcome.decision is PolicyDecision.DENIED
    assert outcome.reason_code is PolicyReasonCode.MANUAL_ACTION
    assert adapter.calls == []
    assert _actions_for(database_session, incident) == []


@pytest.mark.parametrize(
    "deployment",
    [
        _deployment(previous_version=None),
        _deployment(current_version="v1.1.0", previous_version="v1.1.0"),
        _deployment(service="payment-service"),
    ],
)
def test_invalid_deployment_state_is_denied_without_an_action(
    database_session: Session, deployment: DeploymentQueryResult
) -> None:
    incident = _persisted_incident(database_session)
    adapter = RecordingDeploymentAdapter(deployment)

    outcome = PolicyGateService(database_session, adapter).evaluate(_concluded_state(incident))  # type: ignore[arg-type]

    assert outcome.decision is PolicyDecision.DENIED
    assert outcome.reason_code is PolicyReasonCode.INVALID_DEPLOYMENT_STATE
    assert len(adapter.calls) == 1
    assert _actions_for(database_session, incident) == []


def test_unavailable_deployment_adapter_fails_closed_without_an_action(
    database_session: Session,
) -> None:
    incident = _persisted_incident(database_session)
    adapter = RecordingDeploymentAdapter(
        DeploymentAdapterError("fault_lab_unavailable", "Fault Lab is unavailable", retryable=True)
    )

    outcome = PolicyGateService(database_session, adapter).evaluate(_concluded_state(incident))  # type: ignore[arg-type]

    assert outcome.decision is PolicyDecision.DENIED
    assert outcome.reason_code is PolicyReasonCode.DEPLOYMENT_UNAVAILABLE
    assert _actions_for(database_session, incident) == []


def test_invalid_deployment_adapter_response_fails_closed_without_an_action(
    database_session: Session,
) -> None:
    incident = _persisted_incident(database_session)
    adapter = RecordingDeploymentAdapter(
        DeploymentAdapterError(
            "invalid_fault_lab_response",
            "Fault Lab returned an invalid deployment response",
        )
    )

    outcome = PolicyGateService(database_session, adapter).evaluate(_concluded_state(incident))  # type: ignore[arg-type]

    assert outcome.decision is PolicyDecision.DENIED
    assert outcome.reason_code is PolicyReasonCode.DEPLOYMENT_UNAVAILABLE
    assert _actions_for(database_session, incident) == []


def test_non_empty_proposal_parameters_are_denied_and_never_used(database_session: Session) -> None:
    incident = _persisted_incident(database_session)
    state = _concluded_state(incident)
    assert state["proposed_action"] is not None
    state["proposed_action"] = state["proposed_action"].model_copy(
        update={"parameters": {"service": "payment-service", "target_version": "attacker-version"}}
    )
    adapter = RecordingDeploymentAdapter(_deployment())

    outcome = PolicyGateService(database_session, adapter).evaluate(state)  # type: ignore[arg-type]

    assert outcome.decision is PolicyDecision.DENIED
    assert outcome.reason_code is PolicyReasonCode.PROPOSAL_PARAMETERS_NOT_EMPTY
    assert adapter.calls == []
    assert _actions_for(database_session, incident) == []


def test_repeated_policy_evaluation_reuses_one_matching_pending_action(
    database_session: Session,
) -> None:
    incident = _persisted_incident(database_session)
    state = _concluded_state(incident)
    adapter = RecordingDeploymentAdapter(_deployment())
    service = PolicyGateService(database_session, adapter)  # type: ignore[arg-type]

    first = service.evaluate(state)
    second = service.evaluate(state)

    actions = _actions_for(database_session, incident)
    assert first.decision is PolicyDecision.APPROVAL_REQUIRED
    assert second.decision is PolicyDecision.APPROVAL_REQUIRED
    assert second.action_id == first.action_id
    assert len(actions) == 1
    assert actions[0].status == PENDING_APPROVAL


def test_conflicting_pending_action_fails_closed_without_creating_another(
    database_session: Session,
) -> None:
    incident = _persisted_incident(database_session)
    database_session.add(
        Action(
            incident_id=incident.id,
            action_type=ActionType.ROLLBACK_DEPLOYMENT.value,
            status=PENDING_APPROVAL,
            parameters={
                "service": "order-service",
                "environment": "local",
                "current_version": "v1.1.0",
                "target_version": "v0.9.0",
                "reason": "An earlier proposal.",
            },
            executed_at=None,
        )
    )
    database_session.commit()
    adapter = RecordingDeploymentAdapter(_deployment())

    outcome = PolicyGateService(database_session, adapter).evaluate(_concluded_state(incident))  # type: ignore[arg-type]

    assert outcome.decision is PolicyDecision.DENIED
    assert outcome.reason_code is PolicyReasonCode.CONFLICTING_PENDING_ACTION
    assert len(_actions_for(database_session, incident)) == 1


def test_policy_node_only_runs_after_a_concluded_resolution() -> None:
    class UnexpectedPolicyGate:
        def evaluate(self, state: AgentState) -> object:
            raise AssertionError(f"policy must not evaluate incomplete state: {state}")

    now = datetime.now(UTC)
    incident = Incident(
        id=uuid4(),
        service="order-service",
        environment="local",
        description="An incomplete investigation.",
        time_range_start=now,
        time_range_end=now + timedelta(minutes=5),
        thread_id=str(uuid4()),
    )
    state = create_initial_agent_state(incident)

    assert policy_gate_node(state, UnexpectedPolicyGate()) is state  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("decision", "expected_reason"),
    [
        (PolicyDecision.DENIED, TerminalReason.POLICY_DENIED),
        (PolicyDecision.APPROVAL_REQUIRED, None),
    ],
)
def test_policy_node_projects_terminal_reason_only_for_denial(
    decision: PolicyDecision,
    expected_reason: TerminalReason | None,
) -> None:
    class StaticPolicyGate:
        def evaluate(self, state: AgentState) -> PolicyOutcome:
            del state
            return PolicyOutcome(
                decision=decision,
                reason_code=(
                    PolicyReasonCode.MANUAL_ACTION
                    if decision is PolicyDecision.DENIED
                    else PolicyReasonCode.APPROVAL_REQUIRED
                ),
                reason="Controlled policy outcome.",
                action_id=uuid4() if decision is PolicyDecision.APPROVAL_REQUIRED else None,
            )

    now = datetime.now(UTC)
    incident = Incident(
        id=uuid4(),
        service="order-service",
        environment="local",
        description="Policy terminal reason test.",
        time_range_start=now,
        time_range_end=now + timedelta(minutes=5),
        thread_id=str(uuid4()),
    )
    state = _concluded_state(incident)

    updated = policy_gate_node(state, StaticPolicyGate())

    assert updated["policy_outcome"].reason_code is (
        PolicyReasonCode.MANUAL_ACTION
        if decision is PolicyDecision.DENIED
        else PolicyReasonCode.APPROVAL_REQUIRED
    )
    assert updated["terminal_reason"] is expected_reason


def test_policy_service_denies_a_state_not_at_the_conclusion_stage(
    database_session: Session,
) -> None:
    incident = _persisted_incident(database_session)
    state = _concluded_state(incident)
    state["current_stage"] = AgentStage.EVIDENCE_EVALUATION
    adapter = RecordingDeploymentAdapter(_deployment())

    outcome = PolicyGateService(database_session, adapter).evaluate(state)  # type: ignore[arg-type]

    assert outcome.decision is PolicyDecision.DENIED
    assert outcome.reason_code is PolicyReasonCode.INVESTIGATION_NOT_CONCLUDED
    assert adapter.calls == []
    assert _actions_for(database_session, incident) == []


def test_day_three_planner_and_executor_remain_read_only() -> None:
    assert ToolName.ROLLBACK_DEPLOYMENT not in PLANNER_TOOLS
    assert ToolName.ROLLBACK_DEPLOYMENT not in EXECUTOR_TOOLS
