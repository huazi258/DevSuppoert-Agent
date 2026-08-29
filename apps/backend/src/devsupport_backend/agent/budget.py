"""Small, checkpoint-oriented investigation budget and usage boundaries."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from time import monotonic
from typing import Callable, Iterator

from devsupport_backend.agent.llm import LLMClient
from devsupport_backend.agent.state import AgentState


@dataclass(frozen=True)
class InvestigationBudget:
    """Investigation budget dimensions, including the initial V1 discrete limits."""

    max_rounds: int = 5
    max_tool_calls: int = 6
    max_llm_calls: int | None = 8
    max_workflow_retries: int | None = 1
    max_active_execution_seconds: float | None = 95.0

    def __post_init__(self) -> None:
        for name, value in (
            ("max_rounds", self.max_rounds),
            ("max_tool_calls", self.max_tool_calls),
            ("max_llm_calls", self.max_llm_calls),
            ("max_workflow_retries", self.max_workflow_retries),
            ("max_active_execution_seconds", self.max_active_execution_seconds),
        ):
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be greater than zero when set")


DEFAULT_INVESTIGATION_BUDGET = InvestigationBudget()
"""Initial V1 reliability limits, including the calibrated active-execution budget."""


ACTIVE_EXECUTION_SAFETY_MARGIN_SECONDS = 5.0
"""Reserve enough time for checkpointing, routing, and terminal cleanup."""


class ActiveExecutionBudgetExceeded(RuntimeError):
    """An LLM call cannot start or finish within the remaining active-execution budget."""


@dataclass(frozen=True)
class _ActiveExecutionScope:
    """One active graph invocation measured independently from approval wait time."""

    prior_usage_seconds: float
    max_execution_seconds: float | None
    started_at: float
    clock: Callable[[], float]

    def current_usage_seconds(self) -> float:
        return self.prior_usage_seconds + max(0.0, self.clock() - self.started_at)

    def available_llm_seconds(self) -> float | None:
        if self.max_execution_seconds is None:
            return None
        return (
            self.max_execution_seconds
            - self.current_usage_seconds()
            - ACTIVE_EXECUTION_SAFETY_MARGIN_SECONDS
        )


_active_execution_scope: ContextVar[_ActiveExecutionScope | None] = ContextVar(
    "active_execution_scope", default=None
)


@contextmanager
def active_execution_scope(
    prior_usage_seconds: float,
    budget: InvestigationBudget,
    *,
    clock: Callable[[], float] = monotonic,
) -> Iterator[None]:
    """Measure one start, resume, or retry invocation with a monotonic clock."""
    scope = _ActiveExecutionScope(
        prior_usage_seconds=max(0.0, prior_usage_seconds),
        max_execution_seconds=budget.max_active_execution_seconds,
        started_at=clock(),
        clock=clock,
    )
    token = _active_execution_scope.set(scope)
    try:
        yield
    finally:
        _active_execution_scope.reset(token)


def current_active_execution_seconds(state: AgentState) -> float:
    """Return persisted usage plus this invocation's monotonic active elapsed time."""
    persisted_usage = float(state.get("active_execution_seconds", 0.0))
    scope = _active_execution_scope.get()
    if scope is None:
        return persisted_usage
    return max(persisted_usage, scope.current_usage_seconds())


def active_execution_budget_exhausted(state: AgentState, budget: InvestigationBudget) -> bool:
    """Check the active-work boundary without counting human approval wait time."""
    limit = budget.max_active_execution_seconds
    return limit is not None and current_active_execution_seconds(state) >= limit


def effective_llm_timeout_seconds(configured_timeout_seconds: float) -> float:
    """Bound one provider wait by remaining active budget without widening its config timeout."""
    available_seconds = _available_llm_seconds()
    if available_seconds is None:
        return configured_timeout_seconds
    return min(configured_timeout_seconds, available_seconds)


def ensure_llm_execution_admitted() -> None:
    """Reject an LLM call before its provider is invoked when no active time remains."""
    _available_llm_seconds()


def _available_llm_seconds() -> float | None:
    scope = _active_execution_scope.get()
    if scope is None:
        return None
    available_seconds = scope.available_llm_seconds()
    if available_seconds is None:
        return None
    if available_seconds <= 0:
        raise ActiveExecutionBudgetExceeded(
            "active execution budget has no time for another LLM call"
        )
    return available_seconds


def llm_timeout_is_budget_limited(
    configured_timeout_seconds: float, effective_timeout_seconds: float
) -> bool:
    """Identify an active-budget deadline without relying on provider error text."""
    return effective_timeout_seconds < configured_timeout_seconds


@dataclass
class _LLMUsageScope:
    call_count: int = 0


_active_llm_usage_scope: ContextVar[_LLMUsageScope | None] = ContextVar(
    "active_llm_usage_scope", default=None
)


@contextmanager
def collect_llm_usage() -> Iterator[_LLMUsageScope]:
    """Collect provider attempts during one graph-node invocation without altering the contract."""
    scope = _LLMUsageScope()
    token = _active_llm_usage_scope.set(scope)
    try:
        yield scope
    finally:
        _active_llm_usage_scope.reset(token)


class UsageAccountingLLMClient:
    """Count issued LLM attempts for the active graph node while preserving ``LLMClient``."""

    def __init__(self, delegate: LLMClient) -> None:
        self._delegate = delegate

    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        ensure_llm_execution_admitted()
        scope = _active_llm_usage_scope.get()
        if scope is not None:
            scope.call_count += 1
        return self._delegate.complete(system_prompt=system_prompt, user_prompt=user_prompt)
