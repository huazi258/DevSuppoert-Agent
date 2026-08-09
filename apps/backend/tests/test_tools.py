from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from devsupport_backend.models import KnowledgeChunk, KnowledgeDocument
from devsupport_backend.rag.retrieval import RAGService
from devsupport_backend.tools.registry import ToolName, UnknownToolError, tool_registry
from devsupport_backend.tools.schemas import (
    GetDeploymentHistoryInput,
    GetDeploymentHistoryOutput,
    QueryLogsInput,
    QueryLogsOutput,
    QueryMetricsInput,
    QueryMetricsOutput,
    QueryTracesInput,
    QueryTracesOutput,
    RollbackDeploymentInput,
    RollbackDeploymentOutput,
    SearchKnowledgeInput,
    SearchKnowledgeOutput,
    ToolError,
    ToolStatus,
)
from devsupport_backend.tools.search_knowledge import search_knowledge


class FixedEmbeddingClient:
    """Inject a same-dimension vector for live PostgreSQL tool tests."""

    def __init__(self, vector: list[float]) -> None:
        self.vector = vector

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        assert len(texts) == 1
        return [self.vector]


def test_registry_lists_only_the_six_fixed_v0_tools() -> None:
    definitions = tool_registry.list()

    assert [definition.name for definition in definitions] == list(ToolName)
    assert {definition.name for definition in definitions} == {
        ToolName.SEARCH_KNOWLEDGE,
        ToolName.QUERY_LOGS,
        ToolName.QUERY_METRICS,
        ToolName.QUERY_TRACES,
        ToolName.GET_DEPLOYMENT_HISTORY,
        ToolName.ROLLBACK_DEPLOYMENT,
    }
    assert tool_registry.get("search_knowledge").implemented
    assert tool_registry.get("query_metrics").implemented
    assert tool_registry.get("query_traces").implemented
    assert tool_registry.get("get_deployment_history").implemented
    assert tool_registry.get("rollback_deployment").implemented
    assert all(
        blocked_name not in {definition.name.value for definition in definitions}
        for blocked_name in ("shell", "sql", "http", "url")
    )


def test_registry_rejects_unknown_tool_names() -> None:
    with pytest.raises(UnknownToolError, match="tool is not registered"):
        tool_registry.get("run_shell")


def test_all_tool_input_and_output_models_validate() -> None:
    now = datetime.now(UTC)
    unavailable = ToolError(code="not_implemented", message="Adapter is not implemented yet")
    inputs = (
        SearchKnowledgeInput(query="payment timeout", top_k=3),
        QueryLogsInput(
            service="order-service",
            environment="local",
            time_range_start=now,
            time_range_end=now + timedelta(minutes=5),
        ),
        QueryMetricsInput(
            service="order-service",
            environment="local",
        ),
        QueryTracesInput(
            service="order-service",
            environment="local",
            time_range_start=now,
            time_range_end=now + timedelta(minutes=5),
        ),
        GetDeploymentHistoryInput(service="order-service", environment="local"),
        RollbackDeploymentInput(
            service="order-service",
            environment="local",
            target_version="v1.0.0",
            reason="Validated regression",
            approval_id=uuid4(),
        ),
    )
    outputs = (
        SearchKnowledgeOutput(status=ToolStatus.SUCCESS, duration_ms=1.0),
        QueryLogsOutput(status=ToolStatus.UNAVAILABLE, error=unavailable),
        QueryMetricsOutput(status=ToolStatus.UNAVAILABLE, error=unavailable),
        QueryTracesOutput(status=ToolStatus.UNAVAILABLE, error=unavailable),
        GetDeploymentHistoryOutput(status=ToolStatus.UNAVAILABLE, error=unavailable),
        RollbackDeploymentOutput(
            status=ToolStatus.UNAVAILABLE,
            error=unavailable,
            service="order-service",
            environment="local",
            target_version="v1.0.0",
        ),
    )

    assert len(inputs) == len(outputs) == len(tool_registry.list())
    assert all(tool_input.model_config["extra"] == "forbid" for tool_input in inputs)
    assert all(tool_output.status for tool_output in outputs)


def test_tool_models_reject_invalid_and_arbitrary_parameters() -> None:
    now = datetime.now(UTC)

    with pytest.raises(ValidationError):
        SearchKnowledgeInput(query="   ", top_k=0)
    with pytest.raises(ValidationError, match="time_range_start"):
        QueryLogsInput(
            service="order-service",
            environment="local",
            time_range_start=now + timedelta(minutes=1),
            time_range_end=now,
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RollbackDeploymentInput(
            service="order-service",
            environment="local",
            target_version="v1.0.0",
            reason="Validated regression",
            approval_id=uuid4(),
            command="rm -rf /",
        )
    with pytest.raises(ValidationError, match="must include an error"):
        QueryLogsOutput(status=ToolStatus.FAILURE)


def test_search_knowledge_tool_uses_rag_service_and_preserves_scores_and_citation(
    database_session: Session,
) -> None:
    document = KnowledgeDocument(
        title="Configuration runbook",
        source_path=f"knowledge/tests/{uuid4()}.md",
        content_hash="b" * 64,
        document_type="runbook",
        service="order-service",
        environment="local",
        metadata_data={
            "document_id": "rb-configuration",
            "service": "order-service",
            "environment": "local",
            "document_type": "runbook",
            "source": "tool-test-runbook",
        },
    )
    chunk = KnowledgeChunk(
        chunk_index=0,
        content="Configuration evidence for order service failures.",
        embedding=[1.0, 0.0],
        metadata_data={
            **document.metadata_data,
            "section": "Evidence",
            "chunk_index": 0,
        },
    )
    document.chunks.append(chunk)
    database_session.add(document)
    database_session.commit()
    rag_service = RAGService(database_session, FixedEmbeddingClient([1.0, 0.0]))

    output = search_knowledge(
        SearchKnowledgeInput(
            query="configuration",
            service="order-service",
            environment="local",
            document_type="runbook",
            top_k=3,
        ),
        rag_service,
    )

    assert output.status is ToolStatus.SUCCESS
    assert output.error is None
    assert output.duration_ms is not None
    assert len(output.results) == 1
    result = output.results[0]
    assert result.chunk_id == chunk.id
    assert result.vector_score is not None
    assert result.keyword_score is not None
    assert result.citation.document_id == document.id
    assert result.citation.chunk_id == chunk.id
    assert result.citation.source == "tool-test-runbook"
    assert result.citation.section == "Evidence"


def test_search_knowledge_tool_returns_structured_failure(database_session: Session) -> None:
    document = KnowledgeDocument(
        title="Dimension runbook",
        source_path=f"knowledge/tests/{uuid4()}.md",
        content_hash="c" * 64,
        document_type="runbook",
        service="order-service",
        environment="local",
        metadata_data={
            "document_id": "rb-dimension",
            "service": "order-service",
            "environment": "local",
            "document_type": "runbook",
            "source": "tool-test-runbook",
        },
    )
    document.chunks.append(
        KnowledgeChunk(
            chunk_index=0,
            content="Dimension validation evidence.",
            embedding=[1.0, 0.0],
            metadata_data={
                **document.metadata_data,
                "section": "Evidence",
                "chunk_index": 0,
            },
        )
    )
    database_session.add(document)
    database_session.commit()
    rag_service = RAGService(database_session, FixedEmbeddingClient([1.0, 0.0, 0.0]))

    output = search_knowledge(SearchKnowledgeInput(query="dimension"), rag_service)

    assert output.status is ToolStatus.FAILURE
    assert output.error is not None
    assert output.error.code == "retrieval_error"
    assert "dimension" in output.error.message
    assert output.results == []
