"""Minimal persistent Workflow Service boundary for Day 4.0."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Callable, Protocol, cast

from langgraph.checkpoint.base import ERROR, BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from devsupport_backend.agent.budget import (
    DEFAULT_INVESTIGATION_BUDGET,
    InvestigationBudget,
    active_execution_scope,
    current_active_execution_seconds,
)
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

    def __init__(
        self,
        graph: CompiledStateGraph,
        budget: InvestigationBudget = DEFAULT_INVESTIGATION_BUDGET,
        *,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        self._graph = graph
        self._budget = budget
        self._monotonic_clock = monotonic_clock

    def start(
        self, incident: WorkflowIncidentSource, *, symptoms: list[str] | None = None
    ) -> AgentState:
        """Invoke the graph using the incident's stable, persisted thread identifier."""
        state = create_initial_agent_state(incident, symptoms=symptoms)
        return self._invoke_active_execution(
            incident.thread_id,
            state,
            state.get("active_execution_seconds", 0.0),
        )

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
        state = self.get_state(thread_id)
        return self._invoke_active_execution(
            thread_id, None, state.get("active_execution_seconds", 0.0)
        )

    def record_retry_attempt(self, thread_id: str) -> None:
        """Durably consume one authorized retry attempt before continuing the failed task."""
        snapshot = self._graph.get_state(self.config_for(thread_id))
        checkpointer = self._graph.checkpointer
        if not isinstance(checkpointer, BaseCheckpointSaver):
            raise ValueError("workflow retry usage requires a persistent checkpointer")
        self._update_failed_checkpoint_fields(
            snapshot,
            {"workflow_retry_count": snapshot.values.get("workflow_retry_count", 0) + 1},
        )

    def resume(self, thread_id: str, payload: object) -> AgentState:
        """Resume a future interrupted workflow without interpreting the payload as approval."""
        state = self.get_state(thread_id)
        return self._invoke_active_execution(
            thread_id,
            Command(resume=payload),
            state.get("active_execution_seconds", 0.0),
        )

    def _invoke_active_execution(
        self, thread_id: str | None, workflow_input: object, prior_usage_seconds: float
    ) -> AgentState:
        """Run one active invocation and retain elapsed usage if its task fails."""
        config = self.config_for(thread_id)
        with active_execution_scope(
            prior_usage_seconds,
            self._budget,
            clock=self._monotonic_clock,
        ):
            try:
                return self._graph.invoke(workflow_input, config)
            except Exception:
                self._persist_failed_active_execution_usage(config)
                raise

    def _persist_failed_active_execution_usage(self, config: dict[str, object]) -> None:
        """Best-effort failure accounting that must never replace the workflow exception."""
        try:
            snapshot = self._graph.get_state(config)
            state = cast(AgentState, snapshot.values)
            persisted_usage = float(state.get("active_execution_seconds", 0.0))
            active_usage = current_active_execution_seconds(state)
            if active_usage <= persisted_usage:
                return
            self._update_failed_checkpoint_fields(
                snapshot, {"active_execution_seconds": active_usage}
            )
        except Exception:
            pass

    def _update_failed_checkpoint_fields(self, snapshot, values: dict[str, object]) -> None:
        """Rebuild one failed checkpoint while restoring its exact task continuation."""
        checkpointer = self._graph.checkpointer
        if not isinstance(checkpointer, BaseCheckpointSaver):
            raise ValueError("workflow failed checkpoint update requires a persistent checkpointer")
        failed_tasks = [task for task in snapshot.tasks if task.error is not None]
        if len(failed_tasks) != 1 or not failed_tasks[0].id:
            raise ValueError("workflow failed checkpoint update requires exactly one failed task")
        failed_task = failed_tasks[0]
        update_config = snapshot.config
        update_values = values
        if failed_task.interrupts:
            if snapshot.parent_config is None:
                raise ValueError(
                    "workflow failed checkpoint update requires a predecessor checkpoint"
                )
            update_config = snapshot.parent_config
            update_values = {**snapshot.values, **values}
        updated_config = self._graph.update_state(update_config, update_values)
        updated_snapshot = self._graph.get_state(updated_config)
        continued_tasks = [task for task in updated_snapshot.tasks if task.name == failed_task.name]
        if len(continued_tasks) != 1 or not continued_tasks[0].id:
            raise ValueError("workflow failed checkpoint update could not preserve the failed task")
        checkpointer.put_writes(
            updated_snapshot.config,
            [(ERROR, failed_task.error)],
            continued_tasks[0].id,
        )

    @staticmethod
    def config_for(thread_id: str | None) -> dict[str, object]:
        """Build the bounded LangGraph config for one persisted incident thread."""
        if thread_id is None or not thread_id.strip():
            raise ValueError("a persisted incident thread_id is required")
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": DEFAULT_WORKFLOW_RECURSION_LIMIT,
        }
