"""Minimal persistent Workflow Service boundary for Day 4.0."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from devsupport_backend.agent.state import (
    AgentState,
    IncidentStateSource,
    create_initial_agent_state,
)

DEFAULT_WORKFLOW_RECURSION_LIMIT = 40
"""Enough bounded graph steps for five successful rounds and one terminal path."""


@dataclass(frozen=True)
class WorkflowFailure:
    """Safe internal projection of one persisted LangGraph task failure."""

    failed_node: str
    safe_error: str


class WorkflowIncidentSource(IncidentStateSource, Protocol):
    """Incident projection with the persisted identity for its LangGraph thread."""

    thread_id: str | None


class WorkflowService:
    """Start, inspect, and later resume one persisted LangGraph workflow thread."""

    def __init__(self, graph: CompiledStateGraph) -> None:
        self._graph = graph

    def start(
        self, incident: WorkflowIncidentSource, *, symptoms: list[str] | None = None
    ) -> AgentState:
        """Invoke the graph using the incident's stable, persisted thread identifier."""
        result = self._graph.invoke(
            create_initial_agent_state(incident, symptoms=symptoms),
            self.config_for(incident.thread_id),
        )
        return result

    def get_state(self, thread_id: str) -> AgentState:
        """Return the latest checkpointed state for one workflow thread."""
        return self._graph.get_state(self.config_for(thread_id)).values

    def get_failure(self, thread_id: str) -> WorkflowFailure | None:
        """Project exactly one persisted task failure without exposing its raw error."""
        snapshot = self._graph.get_state(self.config_for(thread_id))
        failed_tasks = [task for task in snapshot.tasks if task.error is not None]
        if len(failed_tasks) != 1:
            return None

        failed_task = failed_tasks[0]
        if not failed_task.name:
            return None
        return WorkflowFailure(
            failed_node=failed_task.name,
            safe_error="Persisted workflow task failed",
        )

    def retry_failed_task(self, thread_id: str) -> AgentState:
        """Continue one persisted thread from its failed LangGraph task."""
        result = self._graph.invoke(None, self.config_for(thread_id))
        return result

    def record_retry_attempt(self, thread_id: str) -> None:
        """Durably consume one authorized retry attempt before continuing the failed task."""
        state = self.get_state(thread_id)
        self._graph.update_state(
            self.config_for(thread_id),
            {"workflow_retry_count": state["workflow_retry_count"] + 1},
        )

    def resume(self, thread_id: str, payload: object) -> AgentState:
        """Resume a future interrupted workflow without interpreting the payload as approval."""
        result = self._graph.invoke(Command(resume=payload), self.config_for(thread_id))
        return result

    @staticmethod
    def config_for(thread_id: str | None) -> dict[str, object]:
        """Build the bounded LangGraph config for one persisted incident thread."""
        if thread_id is None or not thread_id.strip():
            raise ValueError("a persisted incident thread_id is required")
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": DEFAULT_WORKFLOW_RECURSION_LIMIT,
        }
