"""Web workflow projection and lifecycle tests without external providers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from devsupport_backend.agent.budget import InvestigationBudget
from devsupport_backend.agent.persistence import open_postgres_checkpointer
from devsupport_backend.agent.runtime import WorkflowFailure, WorkflowService
from devsupport_backend.agent.state import (
    ActionExecutionOutcome,
    ActionType,
    AgentStage,
    AgentState,
    EvidenceContext,
    FailureCategory,
    FinalConclusion,
    HypothesisContext,
    HypothesisStatus,
    PendingToolCall,
    PolicyDecision,
    PolicyOutcome,
    PolicyReasonCode,
    ProposedAction,
    TerminalReason,
    ToolHistoryEntry,
    VerificationOutcome,
    VerificationStatus,
    create_initial_agent_state,
)
from devsupport_backend.models import Action, Approval, Incident
from devsupport_backend.tools.registry import ToolName
from devsupport_backend.tools.schemas import ToolStatus
from devsupport_backend.workflow_console import (
    PostgresWorkflowRuntime,
    WorkflowConflictError,
    WorkflowConsoleService,
    WorkflowRetryError,
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
        retry_error: Exception | None = None,
        failure: WorkflowFailure | None = None,
    ) -> None:
        self.state = state
        self.states = states or []
        self.start_error = start_error
        self.retry_error = retry_error
        self.failure = failure
        self.start_calls = 0
        self.retry_calls = 0
        self.retry_usage_calls = 0
        self.thread_ids: list[str] = []
        self.failure_thread_ids: list[str] = []

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

    def get_failure(self, thread_id: str) -> WorkflowFailure | None:
        self.failure_thread_ids.append(thread_id)
        return self.failure

    def retry_failed_task(self, thread_id: str):
        self.retry_calls += 1
        self.thread_ids.append(thread_id)
        if self.retry_error is not None:
            raise self.retry_error
        if self.state is None:
            raise AssertionError("successful fake retry requires a state")
        return self.state

    def record_retry_attempt(self, thread_id: str) -> None:
        self.retry_usage_calls += 1
        self.thread_ids.append(thread_id)
        if self.state is None:
            raise AssertionError("retry usage requires a persisted state")
        self.state["workflow_retry_count"] += 1


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


def _retryable_failure(failed_node: str) -> WorkflowFailure:
    return WorkflowFailure(
        failed_node=failed_node,
        safe_error="LLM provider request timed out",
        category=FailureCategory.LLM_PROVIDER_TIMEOUT,
        retryable=True,
    )


def test_projector_exposes_only_bound_public_facts(database_session: Session) -> None:
    incident = _incident(database_session)
    action = _action(database_session, incident)

    response = project_workflow_response(incident, _state(incident, action), action)

    body = response.model_dump(mode="json")
    assert body["incident_id"] == str(incident.id)
    assert body["evidence"][0].get("data") is None
    assert "data" not in body["evidence"][0]
    assert body["evidence"][0]["citation"] is None
    assert body["proposed_action"].get("parameters") is None
    assert body["action"]["parameters"] == {
        "service": "order-service",
        "environment": "local",
        "current_version": "v1.1.0",
        "target_version": "v1.0.0",
        "reason": "Verified deployment facts require rollback.",
    }


def test_projector_preserves_only_validated_knowledge_citation(database_session: Session) -> None:
    incident = _incident(database_session)
    state = _state(incident)
    evidence = state["evidence"][0].model_copy(
        update={
            "evidence_type": "knowledge_retrieval",
            "source": "search_knowledge",
            "data": {
                "citation": {
                    "id": "knowledge:runbook#1",
                    "document_id": str(uuid4()),
                    "chunk_id": str(uuid4()),
                    "source": "runbook",
                    "section": "Checks",
                    "document_reference": "knowledge/runbook.md#checks",
                }
            },
        }
    )
    state["evidence"] = [evidence]
    response = project_workflow_response(incident, state, None)
    assert response.evidence[0].citation is not None
    assert response.evidence[0].citation.document_reference == "knowledge/runbook.md#checks"

    state["evidence"] = [evidence.model_copy(update={"data": {"citation": {"private": "nope"}}})]
    assert project_workflow_response(incident, state, None).evidence[0].citation is None


def test_projector_exposes_terminal_reason_and_accepts_legacy_state(
    database_session: Session,
) -> None:
    incident = _incident(database_session)
    state = _state(incident)
    state["terminal_reason"] = TerminalReason.INVESTIGATION_INCONCLUSIVE

    response = project_workflow_response(incident, state, None)

    assert response.terminal_reason is TerminalReason.INVESTIGATION_INCONCLUSIVE

    legacy_state = state.copy()
    del legacy_state["terminal_reason"]
    legacy_response = project_workflow_response(incident, legacy_state, None)

    assert legacy_response.terminal_reason is None


def test_progress_projects_open_and_accepted_incidents_without_checkpoint(
    database_session: Session,
) -> None:
    open_incident = _incident(database_session)
    accepted_incident = _incident(database_session, status="INVESTIGATING")
    runtime = FakeRuntime(state=None)
    service = WorkflowConsoleService(database_session, runtime)

    not_started = service.read_progress(open_incident.id)
    accepted = service.read_progress(accepted_incident.id)

    assert not_started.phase == "not_started"
    assert accepted.phase == "accepted"
    for progress in (not_started, accepted):
        assert progress.checkpoint_available is False
        assert progress.current_stage is None
        assert progress.current_goal is None
        assert progress.pending_tool_name is None
        assert progress.latest_tool is None
        assert progress.failure is None
        assert progress.hypothesis_count == progress.evidence_count == progress.tool_call_count == 0
    assert runtime.start_calls == runtime.retry_calls == 0


def test_progress_projects_running_checkpoint_facts_without_tool_arguments(
    database_session: Session,
) -> None:
    incident = _incident(database_session, status="INVESTIGATING")
    state = _state(incident)
    state.update(
        {
            "current_stage": AgentStage.TOOL_EXECUTION,
            "current_goal": "Collect recent order-service logs.",
            "pending_tool_call": PendingToolCall(
                investigation_goal="Inspect current failures.",
                tool_name=ToolName.QUERY_LOGS,
                tool_arguments={"secret": "not-for-progress"},
                reason="Logs are the next bounded evidence source.",
            ),
            "tool_history": [
                ToolHistoryEntry(
                    tool_name=ToolName.QUERY_METRICS,
                    tool_arguments={"secret": "not-for-progress"},
                    status=ToolStatus.SUCCESS,
                    duration_ms=12.5,
                )
            ],
            "tool_call_count": 3,
            "investigation_round": 2,
            "llm_call_count": 4,
            "workflow_retry_count": 1,
        }
    )
    runtime = FakeRuntime(state=state)

    progress = WorkflowConsoleService(database_session, runtime).read_progress(incident.id)

    assert progress.phase == "running"
    assert progress.checkpoint_available is True
    assert progress.current_stage is AgentStage.TOOL_EXECUTION
    assert progress.current_goal == "Collect recent order-service logs."
    assert progress.pending_tool_name == "query_logs"
    assert progress.hypothesis_count == len(state["hypotheses"])
    assert progress.evidence_count == len(state["evidence"])
    assert progress.tool_call_count == 3
    assert progress.investigation_round == 2
    assert progress.llm_call_count == 4
    assert progress.workflow_retry_count == 1
    assert progress.latest_tool is not None
    assert progress.latest_tool.model_dump() == {
        "tool_name": "query_metrics",
        "status": "success",
        "duration_ms": 12.5,
    }
    assert progress.failure is None
    assert runtime.start_calls == runtime.retry_calls == 0


def test_progress_projects_failure_separately_from_retry_policy(database_session: Session) -> None:
    incident = _incident(database_session, status="INVESTIGATING")
    state = _state(incident)
    state["current_stage"] = AgentStage.INVESTIGATION_PLANNING
    runtime = FakeRuntime(state=state, failure=_retryable_failure("investigation_planning"))

    progress = WorkflowConsoleService(database_session, runtime).read_progress(incident.id)

    assert progress.phase == "failed"
    assert progress.failure is not None
    assert progress.failure.model_dump() == {
        "failed_node": "investigation_planning",
        "category": "LLM_PROVIDER_TIMEOUT",
        "message": "LLM provider request timed out",
        "retryable": True,
    }
    assert progress.retry_available is True


def test_progress_keeps_failure_retryability_when_retry_budget_is_exhausted(
    database_session: Session,
) -> None:
    incident = _incident(database_session, status="INVESTIGATING")
    state = _state(incident)
    state["current_stage"] = AgentStage.INVESTIGATION_PLANNING
    state["workflow_retry_count"] = 1
    runtime = FakeRuntime(state=state, failure=_retryable_failure("investigation_planning"))

    progress = WorkflowConsoleService(
        database_session,
        runtime,
        budget=InvestigationBudget(max_workflow_retries=1),
    ).read_progress(incident.id)

    assert progress.failure is not None and progress.failure.retryable is True
    assert progress.retry_available is False


def test_progress_projects_waiting_and_terminal_states(database_session: Session) -> None:
    waiting = _incident(database_session, status="WAITING_APPROVAL")
    waiting_state = _state(waiting)
    waiting_state["current_stage"] = AgentStage.WAITING_APPROVAL
    waiting_progress = WorkflowConsoleService(
        database_session, FakeRuntime(state=waiting_state)
    ).read_progress(waiting.id)
    assert waiting_progress.phase == "waiting_approval"

    for status, stage in (
        ("RESOLVED", AgentStage.RESOLVED),
        ("NEEDS_MANUAL_ACTION", AgentStage.NEEDS_MANUAL_ACTION),
    ):
        incident = _incident(database_session, status=status)
        state = _state(incident)
        state["current_stage"] = stage
        state["terminal_reason"] = TerminalReason.INVESTIGATION_INCONCLUSIVE

        progress = WorkflowConsoleService(database_session, FakeRuntime(state=state)).read_progress(
            incident.id
        )

        assert progress.phase == "completed"
        assert progress.terminal_reason is TerminalReason.INVESTIGATION_INCONCLUSIVE


def test_progress_fails_closed_for_checkpoint_or_lifecycle_conflicts(
    database_session: Session,
) -> None:
    incident = _incident(database_session, status="INVESTIGATING")
    mismatched = _state(incident)
    mismatched["incident"] = mismatched["incident"].model_copy(
        update={"service": "payment-service"}
    )
    with pytest.raises(WorkflowStateConflict, match="binding"):
        WorkflowConsoleService(database_session, FakeRuntime(state=mismatched)).read_progress(
            incident.id
        )

    for status in ("WAITING_APPROVAL", "RESOLVED", "NEEDS_MANUAL_ACTION"):
        later_incident = _incident(database_session, status=status)
        with pytest.raises(WorkflowStateConflict, match="requires"):
            WorkflowConsoleService(database_session, FakeRuntime(state=None)).read_progress(
                later_incident.id
            )


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
    assert response.retry_available is False
    assert incident.status == "INVESTIGATING"
    assert runtime.start_calls == 1
    assert runtime.thread_ids[-1] == incident.thread_id
    with pytest.raises(WorkflowConflictError):
        service.start(incident.id)
    assert runtime.start_calls == 1


def test_accept_start_marks_open_incident_without_executing_runtime(
    database_session: Session,
) -> None:
    incident = _incident(database_session)
    runtime = FakeRuntime(state=None)
    service = WorkflowConsoleService(database_session, runtime)

    response = service.accept_start(incident.id)

    database_session.refresh(incident)
    assert response.incident_id == incident.id
    assert response.incident_status == "INVESTIGATING"
    assert response.accepted is True
    assert incident.status == "INVESTIGATING"
    assert runtime.start_calls == 0

    with pytest.raises(WorkflowConflictError):
        service.accept_start(incident.id)


def test_accept_start_rejects_existing_persisted_checkpoint(database_session: Session) -> None:
    incident = _incident(database_session)
    runtime = FakeRuntime(state=_state(incident))

    with pytest.raises(WorkflowConflictError):
        WorkflowConsoleService(database_session, runtime).accept_start(incident.id)

    database_session.refresh(incident)
    assert incident.status == "OPEN"
    assert runtime.start_calls == 0


def test_execute_accepted_start_uses_the_accepted_thread(database_session: Session) -> None:
    incident = _incident(database_session)
    runtime = FakeRuntime(state=_state(incident), states=[None])
    service = WorkflowConsoleService(database_session, runtime)

    service.accept_start(incident.id)
    service.execute_accepted_start(incident.id)

    assert runtime.start_calls == 1
    assert runtime.thread_ids[-1] == incident.thread_id


def test_execute_accepted_start_requires_an_accepted_incident(database_session: Session) -> None:
    incident = _incident(database_session)
    runtime = FakeRuntime(state=_state(incident))

    with pytest.raises(WorkflowConflictError):
        WorkflowConsoleService(database_session, runtime).execute_accepted_start(incident.id)

    assert runtime.start_calls == 0


def test_start_failure_restores_open_only_without_a_checkpoint(database_session: Session) -> None:
    incident = _incident(database_session)
    runtime = FakeRuntime(start_error=RuntimeError("provider unavailable"))

    with pytest.raises(WorkflowStartError):
        WorkflowConsoleService(database_session, runtime).start(incident.id)

    database_session.refresh(incident)
    assert incident.status == "OPEN"
    assert runtime.start_calls == 1


def test_execute_accepted_start_failure_restores_open_without_checkpoint(
    database_session: Session,
) -> None:
    incident = _incident(database_session)
    runtime = FakeRuntime(start_error=RuntimeError("provider unavailable"))
    service = WorkflowConsoleService(database_session, runtime)

    service.accept_start(incident.id)
    with pytest.raises(WorkflowStartError):
        service.execute_accepted_start(incident.id)

    database_session.refresh(incident)
    assert incident.status == "OPEN"


def test_execute_accepted_start_failure_preserves_persisted_checkpoint(
    database_session: Session,
) -> None:
    incident = _incident(database_session)
    runtime = FakeRuntime(
        states=[None, _state(incident)],
        start_error=RuntimeError("interrupted"),
    )
    service = WorkflowConsoleService(database_session, runtime)

    service.accept_start(incident.id)
    with pytest.raises(WorkflowStartError):
        service.execute_accepted_start(incident.id)

    database_session.refresh(incident)
    assert incident.status == "INVESTIGATING"


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


def test_postgres_runtime_reads_persisted_failed_task_metadata_without_external_providers(
    database_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incident = _incident(database_session, status="INVESTIGATING")
    state = _state(incident)

    def unexpected_provider(*_: object, **__: object) -> None:
        raise AssertionError("workflow failure reads must not initialize external providers")

    import devsupport_backend.workflow_console as workflow_console_module

    monkeypatch.setattr(workflow_console_module, "OpenAICompatibleLLMClient", unexpected_provider)
    monkeypatch.setattr(
        workflow_console_module,
        "OpenAICompatibleEmbeddingClient",
        unexpected_provider,
    )
    monkeypatch.setattr(workflow_console_module, "RAGService", unexpected_provider)
    monkeypatch.setattr(workflow_console_module, "FaultLabLogsAdapter", unexpected_provider)
    monkeypatch.setattr(workflow_console_module, "FaultLabMetricsAdapter", unexpected_provider)
    monkeypatch.setattr(workflow_console_module, "FaultLabTracesAdapter", unexpected_provider)
    monkeypatch.setattr(workflow_console_module, "FaultLabDeploymentAdapter", unexpected_provider)

    def fail_planning(_: AgentState) -> AgentState:
        raise RuntimeError("controlled persisted planning failure")

    try:
        with open_postgres_checkpointer() as checkpointer:
            graph = StateGraph(AgentState)
            graph.add_node("successful_predecessor", lambda current: current)
            graph.add_node("investigation_planning", fail_planning)
            graph.add_edge(START, "successful_predecessor")
            graph.add_edge("successful_predecessor", "investigation_planning")
            graph.add_edge("investigation_planning", END)
            with pytest.raises(RuntimeError, match="controlled persisted planning failure"):
                graph.compile(checkpointer=checkpointer).invoke(
                    state, WorkflowService.config_for(incident.thread_id)
                )

        failure = PostgresWorkflowRuntime(database_session).get_failure(incident.thread_id)

        assert failure is not None
        assert failure.failed_node == "investigation_planning"
        assert failure.safe_error
    finally:
        with open_postgres_checkpointer() as checkpointer:
            checkpointer.delete_thread(incident.thread_id)


def test_postgres_runtime_reads_controlled_action_execution_failure_metadata(
    database_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incident = _incident(database_session, status="INVESTIGATING")
    state = _state(incident)

    def unexpected_provider(*_: object, **__: object) -> None:
        raise AssertionError("workflow failure reads must not initialize external providers")

    import devsupport_backend.workflow_console as workflow_console_module

    monkeypatch.setattr(workflow_console_module, "OpenAICompatibleLLMClient", unexpected_provider)
    monkeypatch.setattr(
        workflow_console_module,
        "OpenAICompatibleEmbeddingClient",
        unexpected_provider,
    )
    monkeypatch.setattr(workflow_console_module, "RAGService", unexpected_provider)
    monkeypatch.setattr(workflow_console_module, "FaultLabLogsAdapter", unexpected_provider)
    monkeypatch.setattr(workflow_console_module, "FaultLabMetricsAdapter", unexpected_provider)
    monkeypatch.setattr(workflow_console_module, "FaultLabTracesAdapter", unexpected_provider)
    monkeypatch.setattr(workflow_console_module, "FaultLabDeploymentAdapter", unexpected_provider)

    def fail_controlled_execution(_: AgentState) -> AgentState:
        raise RuntimeError("controlled action execution failure")

    try:
        with open_postgres_checkpointer() as checkpointer:
            graph = StateGraph(AgentState)
            graph.add_node("controlled_action_execution", fail_controlled_execution)
            graph.add_edge(START, "controlled_action_execution")
            graph.add_edge("controlled_action_execution", END)
            with pytest.raises(RuntimeError, match="controlled action execution failure"):
                graph.compile(checkpointer=checkpointer).invoke(
                    state, WorkflowService.config_for(incident.thread_id)
                )

        failure = PostgresWorkflowRuntime(database_session).get_failure(incident.thread_id)

        assert failure is not None
        assert failure.failed_node == "controlled_action_execution"
    finally:
        with open_postgres_checkpointer() as checkpointer:
            checkpointer.delete_thread(incident.thread_id)


def test_retry_available_requires_exact_failed_preapproval_task(database_session: Session) -> None:
    incident = _incident(database_session, status="INVESTIGATING")
    state = _state(incident)
    state["current_stage"] = AgentStage.INVESTIGATION_PLANNING
    runtime = FakeRuntime(
        state=state,
        failure=_retryable_failure("investigation_planning"),
    )

    response = WorkflowConsoleService(database_session, runtime).read(incident.id)

    assert response.retry_available is True
    assert runtime.failure_thread_ids == [incident.thread_id]
    assert runtime.retry_calls == 0
    assert "failed_node" not in response.model_dump(mode="json")
    assert "safe_error" not in response.model_dump(mode="json")


@pytest.mark.parametrize(
    ("category", "retryable", "expected_available"),
    [
        (FailureCategory.LLM_PROVIDER_TIMEOUT, True, True),
        (FailureCategory.STRUCTURED_OUTPUT_INVALID, True, True),
        (FailureCategory.WORKFLOW_RUNTIME_FAILURE, False, False),
        (FailureCategory.PERSISTENCE_FAILURE, False, False),
    ],
)
def test_retry_availability_requires_classified_failure_retryability(
    database_session: Session,
    category: FailureCategory,
    retryable: bool,
    expected_available: bool,
) -> None:
    incident = _incident(database_session, status="INVESTIGATING")
    state = _state(incident)
    state["current_stage"] = AgentStage.INVESTIGATION_PLANNING
    runtime = FakeRuntime(
        state=state,
        failure=WorkflowFailure(
            failed_node="investigation_planning",
            safe_error="safe classified failure",
            category=category,
            retryable=retryable,
        ),
    )

    response = WorkflowConsoleService(database_session, runtime).read(incident.id)

    assert response.retry_available is expected_available


def test_non_retryable_failure_is_rejected_before_retry_usage_is_recorded(
    database_session: Session,
) -> None:
    incident = _incident(database_session, status="INVESTIGATING")
    state = _state(incident)
    state["current_stage"] = AgentStage.INVESTIGATION_PLANNING
    runtime = FakeRuntime(
        state=state,
        failure=WorkflowFailure(
            failed_node="investigation_planning",
            safe_error="Workflow execution failed",
            category=FailureCategory.WORKFLOW_RUNTIME_FAILURE,
            retryable=False,
        ),
    )
    service = WorkflowConsoleService(database_session, runtime)

    with pytest.raises(WorkflowConflictError, match="not eligible"):
        service.retry(incident.id)

    assert runtime.retry_usage_calls == 0
    assert runtime.retry_calls == 0
    assert state["workflow_retry_count"] == 0


def test_legacy_failure_projection_is_conservatively_not_retryable(
    database_session: Session,
) -> None:
    incident = _incident(database_session, status="INVESTIGATING")
    state = _state(incident)
    state["current_stage"] = AgentStage.INVESTIGATION_PLANNING
    runtime = FakeRuntime(
        state=state,
        failure=WorkflowFailure(
            failed_node="investigation_planning",
            safe_error="Persisted workflow task failed",
        ),
    )
    service = WorkflowConsoleService(database_session, runtime)

    assert service.read(incident.id).retry_available is False
    with pytest.raises(WorkflowConflictError, match="not eligible"):
        service.retry(incident.id)
    assert runtime.retry_usage_calls == 0
    assert runtime.retry_calls == 0


def test_first_eligible_retry_persists_usage_and_second_retry_is_budget_denied(
    database_session: Session,
) -> None:
    incident = _incident(database_session, status="INVESTIGATING")
    state = _state(incident)
    state["current_stage"] = AgentStage.INVESTIGATION_PLANNING
    runtime = FakeRuntime(
        state=state,
        failure=_retryable_failure("investigation_planning"),
    )
    service = WorkflowConsoleService(database_session, runtime)

    service.retry(incident.id)
    assert service.read(incident.id).retry_available is False
    with pytest.raises(WorkflowConflictError, match="not eligible"):
        service.retry(incident.id)

    assert runtime.retry_usage_calls == 1
    assert runtime.retry_calls == 1
    assert state["workflow_retry_count"] == 1


def test_retry_failure_keeps_usage_and_ineligible_retry_does_not_consume_it(
    database_session: Session,
) -> None:
    incident = _incident(database_session, status="INVESTIGATING")
    state = _state(incident)
    state["current_stage"] = AgentStage.INVESTIGATION_PLANNING
    runtime = FakeRuntime(
        state=state,
        retry_error=RuntimeError("controlled retry failure"),
        failure=_retryable_failure("investigation_planning"),
    )
    service = WorkflowConsoleService(database_session, runtime)

    with pytest.raises(WorkflowRetryError, match="Workflow retry failed"):
        service.retry(incident.id)

    assert runtime.retry_usage_calls == 1
    assert state["workflow_retry_count"] == 1
    assert service.read(incident.id).retry_available is False
    with pytest.raises(WorkflowConflictError, match="not eligible"):
        service.retry(incident.id)
    assert runtime.retry_calls == 1
    assert runtime.retry_usage_calls == 1
    runtime.failure = _retryable_failure("policy_gate")
    with pytest.raises(WorkflowConflictError, match="not eligible"):
        service.retry(incident.id)
    assert runtime.retry_usage_calls == 1


@pytest.mark.parametrize(
    "failed_node",
    ["policy_gate", "approval_wait", "controlled_action_execution"],
)
def test_retry_available_rejects_ineligible_node_and_non_investigating_status(
    database_session: Session,
    failed_node: str,
) -> None:
    incident = _incident(database_session, status="INVESTIGATING")
    runtime = FakeRuntime(
        state=_state(incident),
        failure=_retryable_failure(failed_node),
    )

    response = WorkflowConsoleService(database_session, runtime).read(incident.id)
    assert response.retry_available is False

    for status in ("OPEN", "WAITING_APPROVAL", "RESOLVED", "NEEDS_MANUAL_ACTION"):
        incident.status = status
        database_session.commit()
        runtime.failure = _retryable_failure("investigation_planning")

        response = WorkflowConsoleService(database_session, runtime).read(incident.id)
        assert response.retry_available is False


def test_retry_available_rejects_persisted_action_approval_and_postapproval_outcomes(
    database_session: Session,
) -> None:
    failure = _retryable_failure("investigation_planning")

    action_incident = _incident(database_session, status="INVESTIGATING")
    _action(database_session, action_incident)
    assert (
        WorkflowConsoleService(
            database_session,
            FakeRuntime(state=_state(action_incident), failure=failure),
        )
        .read(action_incident.id)
        .retry_available
        is False
    )

    approval_incident = _incident(database_session, status="INVESTIGATING")
    approval_action = _action(database_session, approval_incident)
    database_session.add(
        Approval(
            incident_id=approval_incident.id,
            action_id=approval_action.id,
            status="APPROVED",
        )
    )
    database_session.commit()
    assert (
        WorkflowConsoleService(
            database_session,
            FakeRuntime(state=_state(approval_incident), failure=failure),
        )
        .read(approval_incident.id)
        .retry_available
        is False
    )

    execution_incident = _incident(database_session, status="INVESTIGATING")
    execution_state = _state(execution_incident)
    execution_state["execution_outcome"] = ActionExecutionOutcome(
        status=ToolStatus.FAILURE,
        executed=False,
    )
    assert (
        WorkflowConsoleService(
            database_session,
            FakeRuntime(state=execution_state, failure=failure),
        )
        .read(execution_incident.id)
        .retry_available
        is False
    )

    verification_incident = _incident(database_session, status="INVESTIGATING")
    verification_state = _state(verification_incident)
    verification_state["verification_outcome"] = VerificationOutcome(
        status=VerificationStatus.INCONCLUSIVE,
        summary="Verification state already exists.",
    )
    assert (
        WorkflowConsoleService(
            database_session,
            FakeRuntime(state=verification_state, failure=failure),
        )
        .read(verification_incident.id)
        .retry_available
        is False
    )
