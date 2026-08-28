"""Small, checkpoint-oriented investigation budget and usage boundaries."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

from devsupport_backend.agent.llm import LLMClient


@dataclass(frozen=True)
class InvestigationBudget:
    """All investigation budget dimensions; unset values are intentionally not enforced yet."""

    max_rounds: int = 5
    max_tool_calls: int = 6
    max_llm_calls: int | None = None
    max_workflow_retries: int | None = None
    max_active_execution_seconds: float | None = None

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
"""Current V0 round/tool limits with later budget dimensions explicitly unset."""


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
        scope = _active_llm_usage_scope.get()
        if scope is not None:
            scope.call_count += 1
        return self._delegate.complete(system_prompt=system_prompt, user_prompt=user_prompt)
