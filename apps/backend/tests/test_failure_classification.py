"""Typed workflow failure taxonomy tests without external providers or persistence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from psycopg import OperationalError as PsycopgOperationalError
from sqlalchemy.exc import OperationalError

from devsupport_backend.agent.failure import classify_workflow_failure
from devsupport_backend.agent.llm import LLMError, LLMProviderTimeoutError
from devsupport_backend.agent.nodes.hypothesis_update import (
    HypothesisUpdateError,
    hypothesis_update_node,
)
from devsupport_backend.agent.nodes.tool_execution import ToolExecutionError
from devsupport_backend.agent.state import AgentStage, FailureCategory
from devsupport_backend.rag.embeddings import EmbeddingError
from devsupport_backend.rag.retrieval import RetrievalError
from devsupport_backend.tools.logs import LogsAdapterError


def test_configured_provider_timeout_has_a_typed_retryable_failure_category() -> None:
    try:
        raise LLMProviderTimeoutError(
            "raw provider payload must not escape"
        ) from httpx.ReadTimeout("network stalled")
    except LLMProviderTimeoutError as error:
        classification = classify_workflow_failure(error)

    assert classification.category is FailureCategory.LLM_PROVIDER_TIMEOUT
    assert classification.retryable is True
    assert classification.safe_message == "LLM provider request timed out"
    assert "raw provider payload" not in classification.safe_message


def test_generic_provider_error_is_retryable_without_exposing_raw_message() -> None:
    classification = classify_workflow_failure(LLMError("api-key=not-safe-to-store"))

    assert classification.category is FailureCategory.LLM_PROVIDER_ERROR
    assert classification.retryable is True
    assert classification.safe_message == "LLM provider request failed"
    assert "api-key" not in classification.safe_message


def test_structured_output_node_wrapper_preserves_its_typed_cause() -> None:
    class InvalidOutputLLM:
        def complete(self, *, system_prompt: str, user_prompt: str) -> str:
            del system_prompt, user_prompt
            return "not structured output"

    from devsupport_backend.agent.state import create_initial_agent_state
    from devsupport_backend.models import Incident

    now = datetime.now(UTC)
    incident = Incident(
        id=uuid4(),
        service="order-service",
        environment="local",
        description="Structured output classification test.",
        time_range_start=now,
        time_range_end=now + timedelta(minutes=5),
        thread_id=str(uuid4()),
    )
    state = create_initial_agent_state(incident)
    state["current_stage"] = AgentStage.HYPOTHESIS_UPDATE

    with pytest.raises(HypothesisUpdateError) as raised:
        hypothesis_update_node(state, InvalidOutputLLM())

    classification = classify_workflow_failure(raised.value)

    assert classification.category is FailureCategory.STRUCTURED_OUTPUT_INVALID
    assert classification.retryable is True


@pytest.mark.parametrize(
    ("error", "category", "retryable"),
    [
        (
            RetrievalError("invalid local query"),
            FailureCategory.RETRIEVAL_FAILURE,
            False,
        ),
        (
            RetrievalError("embedding unavailable"),
            FailureCategory.RETRIEVAL_FAILURE,
            True,
        ),
        (
            LogsAdapterError("temporary", "adapter unavailable", retryable=True),
            FailureCategory.TOOL_FAILURE,
            True,
        ),
        (
            ToolExecutionError("invalid persisted tool arguments"),
            FailureCategory.TOOL_FAILURE,
            False,
        ),
        (
            OperationalError("select 1", {}, RuntimeError("database unavailable")),
            FailureCategory.PERSISTENCE_FAILURE,
            False,
        ),
        (
            PsycopgOperationalError("checkpoint database unavailable"),
            FailureCategory.PERSISTENCE_FAILURE,
            False,
        ),
        (RuntimeError("unexpected"), FailureCategory.WORKFLOW_RUNTIME_FAILURE, False),
    ],
)
def test_classifier_uses_typed_error_facts(
    error: BaseException,
    category: FailureCategory,
    retryable: bool,
) -> None:
    if isinstance(error, RetrievalError) and error.args[0] == "embedding unavailable":
        error.__cause__ = EmbeddingError("provider unavailable")

    classification = classify_workflow_failure(error)

    assert classification.category is category
    assert classification.retryable is retryable
