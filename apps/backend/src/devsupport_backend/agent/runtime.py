"""Minimal persistent Workflow Service boundary for Day 4.0."""

from __future__ import annotations

from typing import Protocol

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from devsupport_backend.agent.state import (
    AgentState,
    IncidentStateSource,
    create_initial_agent_state,
)


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

    def resume(self, thread_id: str, payload: object) -> AgentState:
        """Resume a future interrupted workflow without interpreting the payload as approval."""
        result = self._graph.invoke(Command(resume=payload), self.config_for(thread_id))
        return result

    @staticmethod
    def config_for(thread_id: str | None) -> dict[str, dict[str, str]]:
        """Build the sole LangGraph config shape used for a persisted incident thread."""
        if thread_id is None or not thread_id.strip():
            raise ValueError("a persisted incident thread_id is required")
        return {"configurable": {"thread_id": thread_id}}
