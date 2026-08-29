"""Safe application boundary for starting and projecting persisted workflows."""

from __future__ import annotations

from typing import Protocol, cast
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from devsupport_backend.action_execution import ActionExecutionParameters
from devsupport_backend.agent.budget import DEFAULT_INVESTIGATION_BUDGET, InvestigationBudget
from devsupport_backend.agent.evidence_evaluator import LLMEvidenceEvaluator
from devsupport_backend.agent.llm import OpenAICompatibleLLMClient
from devsupport_backend.agent.nodes.tool_execution import ToolExecutionDependencies
from devsupport_backend.agent.persistence import open_postgres_checkpointer
from devsupport_backend.agent.policy import PolicyGateService
from devsupport_backend.agent.runtime import WorkflowFailure, WorkflowService
from devsupport_backend.agent.state import AgentStage, AgentState
from devsupport_backend.agent.workflow import (
    InvestigationWorkflowDependencies,
    build_production_investigation_graph,
)
from devsupport_backend.approvals import ApprovalDecisionService, ApprovalWaitService
from devsupport_backend.config import settings
from devsupport_backend.models import Action, Approval, Incident
from devsupport_backend.rag.embeddings import OpenAICompatibleEmbeddingClient
from devsupport_backend.rag.retrieval import RAGService
from devsupport_backend.schemas.workflows import (
    WorkflowActionParametersResponse,
    WorkflowActionResponse,
    WorkflowApprovalResponse,
    WorkflowEvidenceResponse,
    WorkflowExecutionResponse,
    WorkflowFinalConclusionResponse,
    WorkflowHypothesisResponse,
    WorkflowPolicyResponse,
    WorkflowProposedActionResponse,
    WorkflowReportOutcomeResponse,
    WorkflowResponse,
    WorkflowToolErrorResponse,
    WorkflowToolHistoryResponse,
    WorkflowVerificationResponse,
)
from devsupport_backend.tools.deployments import FaultLabDeploymentAdapter
from devsupport_backend.tools.logs import FaultLabLogsAdapter
from devsupport_backend.tools.metrics import FaultLabMetricsAdapter
from devsupport_backend.tools.traces import FaultLabTracesAdapter


class WorkflowConsoleError(RuntimeError):
    """Base error for safe workflow-console operations."""


class WorkflowConflictError(WorkflowConsoleError):
    """Start would violate the single persisted workflow lifecycle."""


class WorkflowNotStartedError(WorkflowConsoleError):
    """A read requested a workflow checkpoint which does not exist."""


class WorkflowStateConflict(WorkflowConsoleError):
    """Checkpoint facts do not bind to authoritative PostgreSQL facts."""


class WorkflowStartError(WorkflowConsoleError):
    """The production workflow could not complete its start call safely."""


class WorkflowRetryError(WorkflowConsoleError):
    """A persisted workflow retry could not safely continue."""


RETRYABLE_PRE_APPROVAL_NODES = frozenset(
    {
        "retrieval",
        "hypothesis_generation",
        "investigation_planning",
        "tool_execution",
        "hypothesis_update",
        "evidence_evaluation",
        "resolution_proposal",
    }
)
"""Workflow Console policy for the only pre-approval nodes eligible for recovery."""

_POST_APPROVAL_OR_TERMINAL_STAGES = frozenset(
    {
        AgentStage.WAITING_APPROVAL,
        AgentStage.APPROVAL_DECISION,
        AgentStage.ACTION_EXECUTION,
        AgentStage.RECOVERY_VERIFICATION,
        AgentStage.RESOLVED,
        AgentStage.NEEDS_MANUAL_ACTION,
    }
)

_PERSISTED_WORKFLOW_NODE_NAMES = (
    "intake",
    "retrieval",
    "hypothesis_generation",
    "planning_guard",
    "investigation_planning",
    "tool_execution",
    "hypothesis_update",
    "evidence_evaluation",
    "resolution_proposal",
    "policy_gate",
    "approval_wait",
    "approval_interrupt",
    "approval_decision",
    "controlled_action_execution",
    "recovery_verification",
    "final_report",
    "manual_terminalization",
)


class WorkflowRuntime(Protocol):
    def get_state(self, thread_id: str) -> AgentState | None:
        """Return the latest state for one existing thread without mutating it."""

    def get_failure(self, thread_id: str) -> WorkflowFailure | None:
        """Return one safe persisted failed-task projection without mutating it."""

    def start(self, incident: Incident) -> AgentState:
        """Start the official production graph for an already persisted Incident."""

    def retry_failed_task(self, thread_id: str) -> AgentState:
        """Continue a persisted failed thread when a later policy authorizes it."""

    def record_retry_attempt(self, thread_id: str) -> None:
        """Persist one authorized retry attempt before continuing the failed task."""


class PostgresWorkflowRuntime:
    """The only production composition for a new persisted investigation."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_state(self, thread_id: str) -> AgentState | None:
        with open_postgres_checkpointer() as checkpointer:
            snapshot = self._checkpoint_reader_graph(checkpointer).get_state(
                WorkflowService.config_for(thread_id)
            )
        return cast(AgentState, snapshot.values) if snapshot.values else None

    def get_failure(self, thread_id: str) -> WorkflowFailure | None:
        """Read persisted failed-task metadata without composing external providers."""
        with open_postgres_checkpointer() as checkpointer:
            service = WorkflowService(self._checkpoint_reader_graph(checkpointer))
            return service.get_failure(thread_id)

    def start(self, incident: Incident) -> AgentState:
        with open_postgres_checkpointer() as checkpointer:
            return WorkflowService(self._production_graph(checkpointer)).start(incident)

    def retry_failed_task(self, thread_id: str) -> AgentState:
        """Expose the generic continuation primitive for a later policy-owned caller."""
        with open_postgres_checkpointer() as checkpointer:
            service = WorkflowService(self._production_graph(checkpointer))
            return service.retry_failed_task(thread_id)

    def record_retry_attempt(self, thread_id: str) -> None:
        """Record retry usage in the existing LangGraph checkpoint, without a database table."""
        with open_postgres_checkpointer() as checkpointer:
            service = WorkflowService(self._checkpoint_reader_graph(checkpointer))
            service.record_retry_attempt(thread_id)

    @staticmethod
    def _checkpoint_reader_graph(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
        """Register production node names so LangGraph can project persisted task metadata."""
        graph = StateGraph(AgentState)
        for node_name in _PERSISTED_WORKFLOW_NODE_NAMES:
            graph.add_node(node_name, lambda state: state)
        graph.add_edge(START, _PERSISTED_WORKFLOW_NODE_NAMES[0])
        for current, following in zip(
            _PERSISTED_WORKFLOW_NODE_NAMES,
            _PERSISTED_WORKFLOW_NODE_NAMES[1:],
        ):
            graph.add_edge(current, following)
        graph.add_edge(_PERSISTED_WORKFLOW_NODE_NAMES[-1], END)
        return graph.compile(checkpointer=checkpointer)

    def _production_graph(self, checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
        llm_client = OpenAICompatibleLLMClient.from_settings(settings)
        embedding_client = OpenAICompatibleEmbeddingClient.from_settings(settings)
        rag_service = RAGService(self._session, embedding_client)
        dependencies = InvestigationWorkflowDependencies(
            rag_service=rag_service,
            llm_client=llm_client,
            tool_execution=ToolExecutionDependencies(
                rag_service=rag_service,
                logs_adapter=FaultLabLogsAdapter.from_settings(),
                metrics_adapter=FaultLabMetricsAdapter.from_settings(),
                traces_adapter=FaultLabTracesAdapter.from_settings(),
                deployment_adapter=FaultLabDeploymentAdapter.from_settings(),
            ),
            evaluator=LLMEvidenceEvaluator(llm_client),
            policy_gate=PolicyGateService(self._session, FaultLabDeploymentAdapter.from_settings()),
            approval_wait=ApprovalWaitService(self._session),
            approval_decision=ApprovalDecisionService(self._session),
        )
        return build_production_investigation_graph(
            dependencies,
            session=self._session,
            checkpointer=checkpointer,
        )


class WorkflowConsoleService:
    """Own start conflict protection and read-only public workflow projection."""

    def __init__(
        self,
        session: Session,
        runtime: WorkflowRuntime,
        budget: InvestigationBudget = DEFAULT_INVESTIGATION_BUDGET,
    ) -> None:
        self._session = session
        self._runtime = runtime
        self._budget = budget

    def read(self, incident_id: UUID) -> WorkflowResponse:
        incident = self._get_incident(incident_id)
        state = self._read_state(incident)
        action = self._action_for_state(incident, state)
        return project_workflow_response(
            incident,
            state,
            action,
            retry_available=self._retry_available(incident, state),
        )

    def start(self, incident_id: UUID) -> WorkflowResponse:
        incident = self._session.scalar(
            select(Incident).where(Incident.id == incident_id).with_for_update()
        )
        if incident is None:
            raise LookupError("Incident not found")
        if (
            incident.status != "OPEN"
            or not incident.thread_id
            or not incident.thread_id.strip()
            or self._runtime.get_state(incident.thread_id) is not None
        ):
            raise WorkflowConflictError("Workflow cannot be started for this Incident")
        incident.status = "INVESTIGATING"
        self._session.commit()
        try:
            state = self._runtime.start(incident)
        except Exception as error:
            try:
                checkpoint = self._runtime.get_state(incident.thread_id)
            except Exception:
                raise WorkflowStartError("Workflow start failed") from error
            if checkpoint is None:
                self._session.refresh(incident)
                incident.status = "OPEN"
                self._session.commit()
            raise WorkflowStartError("Workflow start failed") from error
        self._session.refresh(incident)
        return project_workflow_response(incident, state, self._action_for_state(incident, state))

    def retry(self, incident_id: UUID) -> WorkflowResponse:
        """Revalidate and continue exactly one eligible persisted pre-approval failure."""
        incident = self._session.scalar(
            select(Incident).where(Incident.id == incident_id).with_for_update()
        )
        if incident is None:
            raise LookupError("Incident not found")
        if not incident.thread_id or not incident.thread_id.strip():
            raise WorkflowConflictError("Incident has no stable workflow thread")
        try:
            state = self._runtime.get_state(incident.thread_id)
        except Exception as error:
            raise WorkflowRetryError("Workflow retry failed") from error
        if state is None:
            raise WorkflowConflictError("Workflow has no persisted checkpoint to retry")
        try:
            failure = self._runtime.get_failure(incident.thread_id)
        except Exception as error:
            raise WorkflowRetryError("Workflow retry failed") from error
        if not self._is_retry_eligible(incident, state, failure):
            raise WorkflowConflictError("Workflow retry is not eligible for this Incident")
        try:
            self._runtime.record_retry_attempt(incident.thread_id)
            result = self._runtime.retry_failed_task(incident.thread_id)
        except Exception as error:
            raise WorkflowRetryError("Workflow retry failed") from error
        self._session.refresh(incident)
        action = self._action_for_state(incident, result)
        return project_workflow_response(
            incident,
            result,
            action,
            retry_available=self._retry_available(incident, result),
        )

    def _get_incident(self, incident_id: UUID) -> Incident:
        incident = self._session.get(Incident, incident_id)
        if incident is None:
            raise LookupError("Incident not found")
        if not incident.thread_id or not incident.thread_id.strip():
            raise WorkflowConflictError("Incident has no stable workflow thread")
        return incident

    def _read_state(self, incident: Incident) -> AgentState:
        state = self._runtime.get_state(incident.thread_id)
        if state is None:
            raise WorkflowNotStartedError("Workflow not started")
        return state

    def _action_for_state(self, incident: Incident, state: AgentState) -> Action | None:
        policy = state["policy_outcome"]
        if policy is None:
            return None
        if policy.action_id is None:
            return None
        return self._session.get(Action, policy.action_id)

    def _retry_available(self, incident: Incident, state: AgentState) -> bool:
        """Fail closed unless persisted pre-approval facts authorize a retry projection."""
        if not incident.thread_id or not incident.thread_id.strip():
            return False
        try:
            failure = self._runtime.get_failure(incident.thread_id)
        except Exception:
            return False
        return self._is_retry_eligible(incident, state, failure)

    def _is_retry_eligible(
        self,
        incident: Incident,
        state: AgentState,
        failure: WorkflowFailure | None,
    ) -> bool:
        """Apply the one authoritative policy shared by read and retry mutation paths."""
        action_exists = self._session.scalar(
            select(Action.id).where(Action.incident_id == incident.id).limit(1)
        ) is not None
        approval_exists = self._session.scalar(
            select(Approval.id).where(Approval.incident_id == incident.id).limit(1)
        ) is not None
        if (
            incident.status != "INVESTIGATING"
            or not incident.thread_id
            or not incident.thread_id.strip()
            or failure is None
            or failure.failed_node not in RETRYABLE_PRE_APPROVAL_NODES
            or not failure.retryable
            or action_exists
            or approval_exists
            or state["approval_outcome"] is not None
            or state["execution_outcome"] is not None
            or state["verification_outcome"] is not None
            or state["current_stage"] in _POST_APPROVAL_OR_TERMINAL_STAGES
            or _retry_budget_exhausted(state, self._budget)
        ):
            return False
        return True


def _retry_budget_exhausted(state: AgentState, budget: InvestigationBudget) -> bool:
    limit = budget.max_workflow_retries
    return limit is not None and state.get("workflow_retry_count", 0) >= limit


def project_workflow_response(
    incident: Incident,
    state: AgentState,
    action: Action | None,
    *,
    retry_available: bool = False,
) -> WorkflowResponse:
    """Project only approved public facts after binding all authoritative identities."""
    _validate_incident_binding(incident, state)
    policy = state["policy_outcome"]
    if policy is None:
        if action is not None:
            raise WorkflowStateConflict("Action exists without a Policy outcome")
    elif policy.action_id is None:
        if action is not None:
            raise WorkflowStateConflict("Policy has no Action binding")
    elif action is None or action.id != policy.action_id or action.incident_id != incident.id:
        raise WorkflowStateConflict("Policy Action binding mismatch")
    return WorkflowResponse(
        incident_id=incident.id,
        incident_status=incident.status,
        current_stage=state["current_stage"],
        hypotheses=[
            WorkflowHypothesisResponse(
                id=item.id,
                summary=item.summary,
                status=item.status.value,
                confidence=item.confidence,
                supporting_evidence_ids=item.supporting_evidence_ids,
                contradicting_evidence_ids=item.contradicting_evidence_ids,
                next_check=item.next_check,
            )
            for item in state["hypotheses"]
        ],
        evidence=[
            WorkflowEvidenceResponse(
                id=item.id,
                evidence_type=item.evidence_type,
                source=item.source,
                summary=item.summary,
                reference=item.reference,
            )
            for item in state["evidence"]
        ],
        tool_history=[
            WorkflowToolHistoryResponse(
                tool_name=item.tool_name.value,
                tool_arguments=item.tool_arguments,
                status=item.status.value,
                duration_ms=item.duration_ms,
                evidence_ids=item.evidence_ids,
                error=(
                    WorkflowToolErrorResponse(
                        code=item.error.code,
                        message=item.error.message,
                        retryable=item.error.retryable,
                    )
                    if item.error
                    else None
                ),
            )
            for item in state["tool_history"]
        ],
        current_goal=state["current_goal"],
        final_conclusion=(
            WorkflowFinalConclusionResponse(**state["final_conclusion"].model_dump())
            if state["final_conclusion"]
            else None
        ),
        proposed_action=(
            WorkflowProposedActionResponse(
                **state["proposed_action"].model_dump(exclude={"parameters"})
            )
            if state["proposed_action"]
            else None
        ),
        policy_outcome=(
            WorkflowPolicyResponse(
                decision=policy.decision.value,
                reason_code=policy.reason_code.value,
                reason=policy.reason,
                action_id=policy.action_id,
            )
            if policy
            else None
        ),
        action=_action_response(action),
        approval_outcome=(
            WorkflowApprovalResponse(
                approval_id=state["approval_outcome"].approval_id,
                action_id=state["approval_outcome"].action_id,
                status=state["approval_outcome"].status.value,
            )
            if state["approval_outcome"]
            else None
        ),
        execution_outcome=(
            WorkflowExecutionResponse(
                action_id=state["execution_outcome"].action_id,
                approval_id=state["execution_outcome"].approval_id,
                status=state["execution_outcome"].status.value,
                service=state["execution_outcome"].service,
                environment=state["execution_outcome"].environment,
                target_version=state["execution_outcome"].target_version,
                executed=state["execution_outcome"].executed,
            )
            if state["execution_outcome"]
            else None
        ),
        verification_outcome=(
            WorkflowVerificationResponse(
                verification_id=state["verification_outcome"].verification_id,
                action_id=state["verification_outcome"].action_id,
                status=state["verification_outcome"].status.value,
                summary=state["verification_outcome"].summary,
            )
            if state["verification_outcome"]
            else None
        ),
        report_outcome=(
            WorkflowReportOutcomeResponse(**state["report_outcome"].model_dump())
            if state["report_outcome"]
            else None
        ),
        retry_available=retry_available,
    )


def _validate_incident_binding(incident: Incident, state: AgentState) -> None:
    checkpoint = state["incident"]
    if (
        checkpoint.id != incident.id
        or checkpoint.service != incident.service
        or checkpoint.environment != incident.environment
        or checkpoint.description != incident.description
        or checkpoint.time_range_start != incident.time_range_start
        or checkpoint.time_range_end != incident.time_range_end
    ):
        raise WorkflowStateConflict("Checkpoint Incident binding mismatch")


def _action_response(action: Action | None) -> WorkflowActionResponse | None:
    if action is None:
        return None
    try:
        parameters = ActionExecutionParameters.model_validate(action.parameters)
    except ValidationError as error:
        raise WorkflowStateConflict("Persisted Action parameters are invalid") from error
    return WorkflowActionResponse(
        action_id=action.id,
        action_type=action.action_type,
        status=action.status,
        parameters=WorkflowActionParametersResponse(**parameters.model_dump()),
        executed_at=action.executed_at,
    )
