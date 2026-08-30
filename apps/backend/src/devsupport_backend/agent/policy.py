"""Code-level rollback Policy Gate that prepares, but never executes, an Action."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from devsupport_backend.agent.state import (
    ActionType,
    AgentStage,
    AgentState,
    EvaluationDecision,
    PolicyDecision,
    PolicyOutcome,
    PolicyReasonCode,
    TerminalReason,
)
from devsupport_backend.models import Action
from devsupport_backend.tools.adapter_contracts import DeploymentQueryResult
from devsupport_backend.tools.deployments import (
    ROLLBACK_SUPPORTED_SERVICES,
    SUPPORTED_ENVIRONMENT,
    DeploymentAdapterError,
    FaultLabDeploymentAdapter,
)
from devsupport_backend.tools.schemas import GetDeploymentHistoryInput

PENDING_APPROVAL = "PENDING_APPROVAL"


class PolicyGate(Protocol):
    """Injected boundary that evaluates one concluded investigation state."""

    def evaluate(self, state: AgentState) -> PolicyOutcome:
        """Return a fail-closed policy outcome without executing remediation."""


class PolicyGateService:
    """Prepare one idempotent rollback Action from trusted Incident and deployment facts."""

    def __init__(self, session: Session, deployment_adapter: FaultLabDeploymentAdapter) -> None:
        self._session = session
        self._deployment_adapter = deployment_adapter

    def evaluate(self, state: AgentState) -> PolicyOutcome:
        """Apply the fixed V0 Policy Gate and persist only a pending Action when allowed."""
        prerequisite_denial = _validate_prerequisites(state)
        if prerequisite_denial is not None:
            return prerequisite_denial

        proposed_action = state["proposed_action"]
        assert proposed_action is not None
        if proposed_action.parameters:
            return _denied(
                PolicyReasonCode.PROPOSAL_PARAMETERS_NOT_EMPTY,
                "ProposedAction parameters are not trusted execution inputs.",
            )
        if proposed_action.action_type == ActionType.MANUAL_ACTION:
            return _denied(
                PolicyReasonCode.MANUAL_ACTION,
                "manual_action is a recommendation only and has no executable Action in V0.",
            )
        if proposed_action.action_type != ActionType.ROLLBACK_DEPLOYMENT:
            return _denied(
                PolicyReasonCode.UNSUPPORTED_ACTION,
                "The proposed action is not an executable V0 rollback.",
            )

        incident = state["incident"]
        if incident.environment == "production":
            return _denied(
                PolicyReasonCode.PRODUCTION_ENVIRONMENT,
                "Rollback is denied for production incidents.",
            )
        if incident.environment != SUPPORTED_ENVIRONMENT:
            return _denied(
                PolicyReasonCode.UNSUPPORTED_ENVIRONMENT,
                f"Rollback is only supported for environment: {SUPPORTED_ENVIRONMENT}.",
            )
        if incident.service not in ROLLBACK_SUPPORTED_SERVICES:
            return _denied(
                PolicyReasonCode.UNSUPPORTED_SERVICE,
                "The incident service is not supported by the local Fault Lab rollback policy.",
            )

        try:
            deployment = self._deployment_adapter.query(
                GetDeploymentHistoryInput(
                    service=incident.service,
                    environment=incident.environment,
                )
            )
        except DeploymentAdapterError:
            return _denied(
                PolicyReasonCode.DEPLOYMENT_UNAVAILABLE,
                "Current deployment state could not be verified.",
            )

        deployment_denial = _validate_deployment(incident.service, deployment)
        if deployment_denial is not None:
            return deployment_denial

        parameters = {
            "service": incident.service,
            "environment": incident.environment,
            "current_version": deployment.current_version,
            "target_version": deployment.previous_version,
            "reason": proposed_action.reason,
        }
        return self._prepare_pending_action(incident.id, parameters)

    def _prepare_pending_action(
        self, incident_id: UUID, parameters: dict[str, str | None]
    ) -> PolicyOutcome:
        pending_actions = list(
            self._session.scalars(
                select(Action).where(
                    Action.incident_id == incident_id,
                    Action.status == PENDING_APPROVAL,
                )
            )
        )
        if pending_actions:
            if len(pending_actions) == 1 and _matches_prepared_action(
                pending_actions[0], parameters
            ):
                return PolicyOutcome(
                    decision=PolicyDecision.APPROVAL_REQUIRED,
                    reason_code=PolicyReasonCode.APPROVAL_REQUIRED,
                    reason="A matching rollback Action is already pending approval.",
                    action_id=pending_actions[0].id,
                )
            return _denied(
                PolicyReasonCode.CONFLICTING_PENDING_ACTION,
                "A conflicting rollback Action is already pending approval for this incident.",
            )

        action = Action(
            incident_id=incident_id,
            action_type=ActionType.ROLLBACK_DEPLOYMENT.value,
            status=PENDING_APPROVAL,
            parameters=parameters,
            executed_at=None,
        )
        self._session.add(action)
        self._session.commit()
        self._session.refresh(action)
        return PolicyOutcome(
            decision=PolicyDecision.APPROVAL_REQUIRED,
            reason_code=PolicyReasonCode.APPROVAL_REQUIRED,
            reason="A verified rollback Action is pending human approval.",
            action_id=action.id,
        )


def policy_gate_node(state: AgentState, policy_gate: PolicyGate) -> AgentState:
    """Evaluate only an already concluded proposal, then end Task 4.1's graph path."""
    if (
        state["current_stage"] != AgentStage.CONCLUSION
        or state["evaluation_decision"] != EvaluationDecision.CONCLUDE
        or state["final_conclusion"] is None
        or state["proposed_action"] is None
    ):
        return state
    outcome = policy_gate.evaluate(state)
    return {
        **state,
        "policy_outcome": outcome,
        "terminal_reason": (
            TerminalReason.POLICY_DENIED
            if outcome.decision is PolicyDecision.DENIED
            else None
        ),
        "current_stage": AgentStage.POLICY_GATE,
    }


def _validate_prerequisites(state: AgentState) -> PolicyOutcome | None:
    if (
        state["current_stage"] != AgentStage.CONCLUSION
        or state["evaluation_decision"] != EvaluationDecision.CONCLUDE
    ):
        return _denied(
            PolicyReasonCode.INVESTIGATION_NOT_CONCLUDED,
            "Policy Gate requires a concluded investigation.",
        )
    if state["final_conclusion"] is None:
        return _denied(
            PolicyReasonCode.MISSING_FINAL_CONCLUSION,
            "Policy Gate requires a FinalConclusion.",
        )
    if state["proposed_action"] is None:
        return _denied(
            PolicyReasonCode.MISSING_PROPOSED_ACTION,
            "Policy Gate requires a ProposedAction.",
        )
    return None


def _validate_deployment(
    incident_service: str, deployment: DeploymentQueryResult
) -> PolicyOutcome | None:
    if (
        deployment.service != incident_service
        or deployment.previous_version is None
        or deployment.previous_version == deployment.current_version
    ):
        return _denied(
            PolicyReasonCode.INVALID_DEPLOYMENT_STATE,
            "Deployment state does not provide a distinct previous version for rollback.",
        )
    return None


def _matches_prepared_action(action: Action, parameters: dict[str, str | None]) -> bool:
    if action.action_type != ActionType.ROLLBACK_DEPLOYMENT.value or action.executed_at is not None:
        return False
    return action.parameters == parameters


def _denied(reason_code: PolicyReasonCode, reason: str) -> PolicyOutcome:
    return PolicyOutcome(
        decision=PolicyDecision.DENIED,
        reason_code=reason_code,
        reason=reason,
        action_id=None,
    )
