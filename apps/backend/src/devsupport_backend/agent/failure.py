"""Pure classification of safe, persisted workflow failure facts."""

from __future__ import annotations

from dataclasses import dataclass

from psycopg import Error as PsycopgError
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from devsupport_backend.agent.llm import LLMError, LLMProviderTimeoutError
from devsupport_backend.agent.nodes.tool_execution import ToolExecutionError
from devsupport_backend.agent.state import FailureCategory
from devsupport_backend.agent.structured_output import StructuredOutputParseError
from devsupport_backend.rag.embeddings import EmbeddingError
from devsupport_backend.rag.retrieval import RetrievalError
from devsupport_backend.tools.deployments import DeploymentAdapterError
from devsupport_backend.tools.logs import LogsAdapterError
from devsupport_backend.tools.metrics import MetricsAdapterError
from devsupport_backend.tools.traces import TracesAdapterError


@dataclass(frozen=True)
class FailureClassification:
    """Content-free facts suitable for an Agent checkpoint and Eval output."""

    category: FailureCategory
    retryable: bool
    safe_message: str


_SAFE_MESSAGES = {
    FailureCategory.LLM_PROVIDER_TIMEOUT: "LLM provider request timed out",
    FailureCategory.LLM_PROVIDER_ERROR: "LLM provider request failed",
    FailureCategory.STRUCTURED_OUTPUT_INVALID: "LLM returned invalid structured output",
    FailureCategory.RETRIEVAL_FAILURE: "Knowledge retrieval failed",
    FailureCategory.TOOL_FAILURE: "Investigation tool failed",
    FailureCategory.PERSISTENCE_FAILURE: "Workflow persistence failed",
    FailureCategory.WORKFLOW_RUNTIME_FAILURE: "Workflow execution failed",
}


def classify_workflow_failure(error: BaseException) -> FailureClassification:
    """Classify a graph exception from its typed cause chain without parsing messages."""
    chain = tuple(_exception_chain(error))
    if any(isinstance(item, LLMProviderTimeoutError) for item in chain):
        return _classification(FailureCategory.LLM_PROVIDER_TIMEOUT, retryable=True)
    if any(isinstance(item, LLMError) for item in chain):
        return _classification(FailureCategory.LLM_PROVIDER_ERROR, retryable=True)
    if any(
        isinstance(item, (StructuredOutputParseError, ValidationError)) for item in chain
    ):
        return _classification(FailureCategory.STRUCTURED_OUTPUT_INVALID, retryable=True)
    if any(isinstance(item, (RetrievalError, EmbeddingError)) for item in chain):
        return _classification(
            FailureCategory.RETRIEVAL_FAILURE,
            retryable=any(isinstance(item, EmbeddingError) for item in chain),
        )
    tool_errors = (
        DeploymentAdapterError,
        LogsAdapterError,
        MetricsAdapterError,
        TracesAdapterError,
    )
    matching_tool_errors = [
        item for item in chain if isinstance(item, (*tool_errors, ToolExecutionError))
    ]
    if matching_tool_errors:
        return _classification(
            FailureCategory.TOOL_FAILURE,
            retryable=any(bool(getattr(item, "retryable", False)) for item in matching_tool_errors),
        )
    if any(isinstance(item, (PsycopgError, SQLAlchemyError)) for item in chain):
        return _classification(FailureCategory.PERSISTENCE_FAILURE, retryable=False)
    return _classification(FailureCategory.WORKFLOW_RUNTIME_FAILURE, retryable=False)


def _exception_chain(error: BaseException):
    """Yield each explicit cause or implicit context once, with a conservative bound."""
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(seen) < 32:
        yield current
        seen.add(id(current))
        current = current.__cause__ or current.__context__


def _classification(category: FailureCategory, *, retryable: bool) -> FailureClassification:
    return FailureClassification(
        category=category,
        retryable=retryable,
        safe_message=safe_message_for_failure_category(category),
    )


def safe_message_for_failure_category(category: FailureCategory) -> str:
    """Return the one stable public-safe message for a persisted category."""
    return _SAFE_MESSAGES[category]
