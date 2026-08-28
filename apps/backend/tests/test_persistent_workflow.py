"""PostgreSQL persistence tests for the Day 4.0 Workflow Service boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TypedDict
from uuid import uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from devsupport_backend.agent.persistence import open_postgres_checkpointer, psycopg_dsn
from devsupport_backend.agent.runtime import WorkflowService
from devsupport_backend.agent.state import (
    AgentStage,
    AgentState,
    EvaluationDecision,
    EvidenceContext,
    FinalConclusion,
    HypothesisContext,
    HypothesisStatus,
    ProposedAction,
    ToolHistoryEntry,
    create_initial_agent_state,
)
from devsupport_backend.config import settings
from devsupport_backend.models import Incident
from devsupport_backend.tools.schemas import ToolStatus


class InterruptState(TypedDict):
    """Minimal test-only state used to prove persisted interrupt/resume behavior."""

    before_interrupt: int
    result: int | None


def _build_incident(*, thread_id: str | None = None) -> Incident:
    now = datetime.now(UTC)
    return Incident(
        id=uuid4(),
        service="order-service",
        environment="local",
        description="A test incident for persisted workflow state.",
        time_range_start=now,
        time_range_end=now + timedelta(minutes=5),
        thread_id=thread_id or str(uuid4()),
    )


def _complete_agent_state(incident: Incident) -> AgentState:
    state = create_initial_agent_state(incident, symptoms=["POST /orders returns 500"])
    evidence = EvidenceContext(
        evidence_type="metric_snapshot",
        source="query_metrics",
        summary="Order-service reports a non-zero error rate.",
        data={"error_rate": 0.5},
    )
    hypothesis = HypothesisContext(
        summary="A deployment-related configuration condition affects order-service.",
        status=HypothesisStatus.CONFIRMED,
        confidence=0.8,
        supporting_evidence_ids=[evidence.id],
    )
    state.update(
        {
            "current_stage": AgentStage.CONCLUSION,
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
            "llm_call_count": 4,
            "workflow_retry_count": 1,
            "evaluation_decision": EvaluationDecision.CONCLUDE,
            "proposed_action": ProposedAction(
                action_type="manual_action",
                summary="Request operator review of the confirmed evidence.",
                reason="The hypothesis is supported by runtime evidence.",
                risk="Any operational change requires later policy review.",
                supporting_evidence_ids=[evidence.id],
            ),
            "final_conclusion": FinalConclusion(
                summary="The evidence supports a deployment-related condition.",
                root_cause=hypothesis.summary,
                confidence=hypothesis.confidence,
                supporting_evidence_ids=[evidence.id],
                recommended_next_action="Request operator review.",
            ),
        }
    )
    return state


def _agent_state_graph(*, checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
    graph = StateGraph(AgentState)
    graph.add_node(
        "record_progress",
        lambda state: {"tool_call_count": state["tool_call_count"] + 1},
    )
    graph.add_edge(START, "record_progress")
    graph.add_edge("record_progress", END)
    return graph.compile(checkpointer=checkpointer)


def _interrupt_graph(*, checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
    def pause_for_test(state: InterruptState) -> dict[str, int]:
        value = interrupt({"before_interrupt": state["before_interrupt"]})
        return {
            "before_interrupt": state["before_interrupt"],
            "result": state["before_interrupt"] + int(value),
        }

    graph = StateGraph(InterruptState)
    graph.add_node("pause_for_test", pause_for_test)
    graph.add_edge(START, "pause_for_test")
    graph.add_edge("pause_for_test", END)
    return graph.compile(checkpointer=checkpointer)


def _delete_thread(thread_id: str) -> None:
    with open_postgres_checkpointer() as checkpointer:
        checkpointer.delete_thread(thread_id)


def test_psycopg_dsn_converts_the_single_sqlalchemy_database_url() -> None:
    dsn = psycopg_dsn(settings.database_url)

    assert dsn.startswith("postgresql://")
    assert "+psycopg" not in dsn


def test_workflow_service_starts_and_queries_a_stable_incident_thread() -> None:
    incident = _build_incident()
    try:
        with open_postgres_checkpointer() as checkpointer:
            service = WorkflowService(_agent_state_graph(checkpointer=checkpointer))
            started = service.start(incident, symptoms=["HTTP 500"])
            recovered = service.get_state(incident.thread_id)

        assert started["incident"].id == incident.id
        assert recovered["incident"].id == incident.id
        assert recovered["incident"].symptoms == ["HTTP 500"]
        assert recovered["tool_call_count"] == 1
    finally:
        _delete_thread(incident.thread_id)


def test_postgres_checkpoint_recovers_the_complete_current_agent_state() -> None:
    incident = _build_incident()
    state = _complete_agent_state(incident)
    try:
        with open_postgres_checkpointer() as checkpointer:
            graph = _agent_state_graph(checkpointer=checkpointer)
            config = WorkflowService.config_for(incident.thread_id)
            graph.invoke(state, config)
            recovered = WorkflowService(graph).get_state(incident.thread_id)

        assert recovered["incident"] == state["incident"]
        assert recovered["hypotheses"] == state["hypotheses"]
        assert recovered["evidence"] == state["evidence"]
        assert recovered["tool_history"] == state["tool_history"]
        assert recovered["investigation_round"] == state["investigation_round"]
        assert recovered["tool_call_count"] == state["tool_call_count"] + 1
        assert recovered["llm_call_count"] == state["llm_call_count"]
        assert recovered["workflow_retry_count"] == state["workflow_retry_count"]
        assert recovered["evaluation_decision"] == EvaluationDecision.CONCLUDE
        assert recovered["proposed_action"] == state["proposed_action"]
        assert recovered["final_conclusion"] == state["final_conclusion"]
    finally:
        _delete_thread(incident.thread_id)


def test_checkpoint_survives_close_reopen_with_the_same_thread_state() -> None:
    incident = _build_incident()
    state = _complete_agent_state(incident)
    config = WorkflowService.config_for(incident.thread_id)
    try:
        with open_postgres_checkpointer() as first_checkpointer:
            first_graph = _agent_state_graph(checkpointer=first_checkpointer)
            first_graph.invoke(state, config)

        with open_postgres_checkpointer() as second_checkpointer:
            second_graph = _agent_state_graph(checkpointer=second_checkpointer)
            before_continue = second_graph.get_state(config).values

        assert before_continue["incident"] == state["incident"]
        assert before_continue["tool_call_count"] == state["tool_call_count"] + 1
    finally:
        _delete_thread(incident.thread_id)


def test_interrupt_resume_survives_close_reopen_without_approval_semantics() -> None:
    thread_id = str(uuid4())
    config = WorkflowService.config_for(thread_id)
    try:
        with open_postgres_checkpointer() as first_checkpointer:
            first_graph = _interrupt_graph(checkpointer=first_checkpointer)
            interrupted = first_graph.invoke({"before_interrupt": 7, "result": None}, config)
            paused_state = first_graph.get_state(config).values

        with open_postgres_checkpointer() as second_checkpointer:
            second_graph = _interrupt_graph(checkpointer=second_checkpointer)
            service = WorkflowService(second_graph)
            recovered_before_resume = service.get_state(thread_id)
            resumed = service.resume(thread_id, 5)

        assert "__interrupt__" in interrupted
        assert paused_state == {"before_interrupt": 7, "result": None}
        assert recovered_before_resume == paused_state
        assert resumed == {"before_interrupt": 7, "result": 12}
    finally:
        _delete_thread(thread_id)


def test_different_threads_are_isolated_in_postgres_checkpoints() -> None:
    first_incident = _build_incident()
    second_incident = _build_incident()
    try:
        with open_postgres_checkpointer() as checkpointer:
            service = WorkflowService(_agent_state_graph(checkpointer=checkpointer))
            service.start(first_incident, symptoms=["First incident"])
            service.start(second_incident, symptoms=["Second incident"])
            first_state = service.get_state(first_incident.thread_id)
            second_state = service.get_state(second_incident.thread_id)

        assert first_state["incident"].id == first_incident.id
        assert first_state["incident"].symptoms == ["First incident"]
        assert second_state["incident"].id == second_incident.id
        assert second_state["incident"].symptoms == ["Second incident"]
    finally:
        _delete_thread(first_incident.thread_id)
        _delete_thread(second_incident.thread_id)
