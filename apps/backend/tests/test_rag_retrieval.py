from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from devsupport_backend.models import KnowledgeChunk, KnowledgeDocument
from devsupport_backend.rag.retrieval import RAGService, RetrievalError, RetrievalFilters


class QueryEmbeddingClient:
    """Inject deterministic query vectors without making external API calls."""

    def __init__(self, vector: list[float]) -> None:
        self.vector = vector

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        assert len(texts) == 1
        return [self.vector]


def _add_document(
    session: Session,
    *,
    service: str,
    environment: str,
    document_type: str,
    content: str,
    embedding: list[float],
    source: str,
    section: str,
) -> KnowledgeChunk:
    document_reference = f"doc-{uuid4()}"
    document = KnowledgeDocument(
        title=f"{service} {section}",
        source_path=f"knowledge/tests/{uuid4()}.md",
        content_hash="a" * 64,
        document_type=document_type,
        service=service,
        environment=environment,
        metadata_data={
            "document_id": document_reference,
            "service": service,
            "environment": environment,
            "document_type": document_type,
            "source": source,
        },
    )
    chunk = KnowledgeChunk(
        chunk_index=0,
        content=content,
        embedding=embedding,
        metadata_data={
            **document.metadata_data,
            "section": section,
            "chunk_index": 0,
        },
    )
    document.chunks.append(chunk)
    session.add(document)
    session.commit()
    return chunk


def test_hybrid_search_keeps_vector_only_and_keyword_only_candidates(
    database_session: Session,
) -> None:
    vector_only = _add_document(
        database_session,
        service="order-service",
        environment="local",
        document_type="runbook",
        content="Semantic recovery guidance without the query keyword.",
        embedding=[1.0, 0.0],
        source="vector-test",
        section="Vector evidence",
    )
    keyword_only = _add_document(
        database_session,
        service="payment-service",
        environment="local",
        document_type="postmortem",
        content="timeout timeout downstream incident evidence",
        embedding=[0.0, 1.0],
        source="keyword-test",
        section="Keyword evidence",
    )
    for index in range(5):
        _add_document(
            database_session,
            service="order-service",
            environment="local",
            document_type="architecture",
            content=f"Additional semantic candidate {index} without the keyword.",
            embedding=[0.8, 0.2],
            source=f"filler-{index}",
            section="Filler",
        )

    results = RAGService(database_session, QueryEmbeddingClient([1.0, 0.0])).search(
        "timeout", top_k=2
    )
    results_by_chunk = {result.chunk_id: result for result in results}

    assert set(results_by_chunk) == {vector_only.id, keyword_only.id}
    assert results_by_chunk[vector_only.id].vector_score == pytest.approx(1.0)
    assert results_by_chunk[vector_only.id].keyword_score is None
    assert results_by_chunk[keyword_only.id].vector_score is None
    assert results_by_chunk[keyword_only.id].keyword_score is not None
    assert all(result.fusion_score > 0 for result in results)


def test_metadata_filters_include_common_environment_and_complete_citation(
    database_session: Session,
) -> None:
    local_chunk = _add_document(
        database_session,
        service="order-service",
        environment="local",
        document_type="runbook",
        content="local order deployment investigation",
        embedding=[1.0, 0.0],
        source="local-source",
        section="Local",
    )
    common_chunk = _add_document(
        database_session,
        service="order-service",
        environment="common",
        document_type="architecture",
        content="common order deployment architecture",
        embedding=[0.9, 0.1],
        source="common-source",
        section="Common",
    )
    _add_document(
        database_session,
        service="order-service",
        environment="staging",
        document_type="runbook",
        content="staging order deployment investigation",
        embedding=[0.8, 0.2],
        source="staging-source",
        section="Staging",
    )
    _add_document(
        database_session,
        service="payment-service",
        environment="common",
        document_type="runbook",
        content="common payment deployment investigation",
        embedding=[0.7, 0.3],
        source="payment-source",
        section="Payment",
    )

    results = RAGService(database_session, QueryEmbeddingClient([1.0, 0.0])).search(
        "deployment",
        filters=RetrievalFilters(service="order-service", environment="local"),
        top_k=5,
    )

    assert {result.chunk_id for result in results} == {local_chunk.id, common_chunk.id}
    assert {result.environment for result in results} == {"local", "common"}
    citation = next(result.citation for result in results if result.chunk_id == local_chunk.id)
    assert citation.document_id == next(
        result.document_id for result in results if result.chunk_id == local_chunk.id
    )
    assert citation.chunk_id == local_chunk.id
    assert citation.source == "local-source"
    assert citation.section == "Local"
    assert citation.document_reference.startswith("doc-")
    assert citation.id.startswith("knowledge:")

    runbook_results = RAGService(database_session, QueryEmbeddingClient([1.0, 0.0])).search(
        "deployment",
        filters=RetrievalFilters(
            service="order-service",
            environment="local",
            document_type="runbook",
        ),
        top_k=5,
    )
    assert [result.chunk_id for result in runbook_results] == [local_chunk.id]


def test_search_rejects_query_embedding_dimension_mismatch(database_session: Session) -> None:
    _add_document(
        database_session,
        service="order-service",
        environment="local",
        document_type="runbook",
        content="configuration evidence",
        embedding=[1.0, 0.0],
        source="dimension-test",
        section="Dimensions",
    )

    service = RAGService(database_session, QueryEmbeddingClient([1.0, 0.0, 0.0]))

    with pytest.raises(RetrievalError, match="query embedding dimension does not match"):
        service.search("configuration")


def test_search_rejects_mixed_corpus_embedding_dimensions(database_session: Session) -> None:
    _add_document(
        database_session,
        service="order-service",
        environment="local",
        document_type="runbook",
        content="configuration evidence one",
        embedding=[1.0, 0.0],
        source="dimension-one",
        section="Dimensions one",
    )
    _add_document(
        database_session,
        service="order-service",
        environment="local",
        document_type="postmortem",
        content="configuration evidence two",
        embedding=[1.0, 0.0, 0.0],
        source="dimension-two",
        section="Dimensions two",
    )

    service = RAGService(database_session, QueryEmbeddingClient([1.0, 0.0]))

    with pytest.raises(RetrievalError, match="mixed embedding dimensions"):
        service.search("configuration")
