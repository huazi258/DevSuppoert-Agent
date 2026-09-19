"""Deterministic V2 retry and fallback policy for safe runtime failure facts."""

from __future__ import annotations

from dataclasses import dataclass

from devsupport_backend.agent.budget import InvestigationBudget
from devsupport_backend.agent.state import AgentState, RuntimeFailureCategory
from devsupport_backend.tools.registry import ToolName
from devsupport_backend.tools.schemas import ToolError


@dataclass(frozen=True)
class RetryDecision:
    """A bounded runtime decision that never relies on an LLM judgment."""

    retry: bool
    exhausted: bool = False


def classify_tool_error(error: ToolError) -> RuntimeFailureCategory:
    """Map the finite normalized Tool error taxonomy to a V2 runtime category."""
    try:
        return RuntimeFailureCategory(error.code)
    except ValueError:
        return RuntimeFailureCategory.PROVIDER_UNAVAILABLE


def is_retryable(category: RuntimeFailureCategory) -> bool:
    """Return the fixed retry policy for one safe failure category."""
    return category in {
        RuntimeFailureCategory.TIMEOUT,
        RuntimeFailureCategory.PROVIDER_UNAVAILABLE,
        RuntimeFailureCategory.INVALID_PROVIDER_RESPONSE,
        RuntimeFailureCategory.PLANNER_FAILURE,
        RuntimeFailureCategory.STRUCTURED_OUTPUT_FAILURE,
    }


def counts_as_execution_failure(category: RuntimeFailureCategory) -> bool:
    """Count only actual provider execution failures toward the M4.1 streak."""
    return category in {
        RuntimeFailureCategory.TIMEOUT,
        RuntimeFailureCategory.PROVIDER_UNAVAILABLE,
        RuntimeFailureCategory.INVALID_PROVIDER_RESPONSE,
    }


def retry_decision(
    state: AgentState,
    budget: InvestigationBudget,
    category: RuntimeFailureCategory,
    *,
    next_tool_call_count: int | None = None,
) -> RetryDecision:
    """Apply retry, Tool, and iteration budgets before scheduling one retry."""
    if not is_retryable(category):
        return RetryDecision(retry=False)
    limit = budget.max_workflow_retries
    if limit is not None and state.get("retry_count", 0) >= limit:
        return RetryDecision(retry=False, exhausted=True)
    if (
        next_tool_call_count is not None
        and next_tool_call_count >= budget.max_tool_calls
    ):
        return RetryDecision(retry=False)
    if state["investigation_round"] >= budget.iteration_limit:
        return RetryDecision(retry=False)
    return RetryDecision(retry=True)


def exhausted_tool_capabilities(state: AgentState) -> frozenset[ToolName]:
    """Return only capabilities that deployment configuration has explicitly disabled."""
    exhausted: set[ToolName] = set()
    for entry in state["tool_history"]:
        if entry.status.value == "success":
            exhausted.discard(entry.tool_name)
            continue
        if entry.error is not None and (
            classify_tool_error(entry.error) is RuntimeFailureCategory.CAPABILITY_UNAVAILABLE
        ):
            exhausted.add(entry.tool_name)
    return frozenset(exhausted)
