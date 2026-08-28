"""Optional, no-op-safe execution hooks for investigation graph nodes."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from time import perf_counter
from typing import Protocol, TypeVar

from devsupport_backend.agent.state import AgentState

NodeResult = TypeVar("NodeResult")
_active_node_name: ContextVar[str | None] = ContextVar(
    "devsupport_active_investigation_node", default=None
)


class InvestigationNodeObserver(Protocol):
    """Best-effort observer for one LangGraph node execution."""

    def node_started(self, node_name: str) -> None:
        """Record that a graph node began execution."""

    def node_finished(self, node_name: str, duration_ms: float, outcome: str) -> None:
        """Record a completed or failed graph node execution."""


def active_investigation_node() -> str | None:
    """Return the node executing in this context, if optional observation is active."""
    return _active_node_name.get()


def observe_investigation_node(
    node_name: str,
    node: Callable[[AgentState], NodeResult],
    observer: InvestigationNodeObserver | None,
) -> Callable[[AgentState], NodeResult]:
    """Wrap one node only when an observer was explicitly supplied.

    Observer errors are intentionally discarded so diagnostics cannot alter workflow behavior.
    The context variable is reset for every success and exception path.
    """
    if observer is None:
        return node

    def observed_node(state: AgentState) -> NodeResult:
        token = _active_node_name.set(node_name)
        started = perf_counter()
        _notify(lambda: observer.node_started(node_name))
        try:
            result = node(state)
        except BaseException:
            _notify(
                lambda: observer.node_finished(
                    node_name, _elapsed_ms(started), "error"
                )
            )
            raise
        else:
            _notify(
                lambda: observer.node_finished(
                    node_name, _elapsed_ms(started), "completed"
                )
            )
            return result
        finally:
            _active_node_name.reset(token)

    return observed_node


def _notify(callback: Callable[[], None]) -> None:
    try:
        callback()
    except Exception:
        pass


def _elapsed_ms(started: float) -> float:
    return round((perf_counter() - started) * 1000, 2)
