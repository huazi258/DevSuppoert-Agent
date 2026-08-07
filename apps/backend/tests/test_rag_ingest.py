from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from devsupport_backend.database import engine
from devsupport_backend.models import KnowledgeChunk, KnowledgeDocument
from devsupport_backend.rag.ingest import collect_documents, ingest_documents
from devsupport_backend.rag.markdown import KnowledgeDocumentParseError, chunk_markdown


class FakeEmbeddingClient:
    """Deterministic, injectable embedding boundary used without external API calls."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(index + 1), 0.5, 0.25] for index, _ in enumerate(texts)]


@pytest.fixture
def database_session() -> Iterator[Session]:
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def knowledge_dir(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    (root / "runbooks").mkdir(parents=True)
    (root / "architecture").mkdir()
    _write_document(
        root / "runbooks" / "order-500.md",
        document_id="rb-order-500",
        service="order-service",
        environment="local",
        document_type="runbook",
        source="test-runbook",
        title="Order 500 triage",
        sections={
            "Symptoms": "Check configuration errors before assuming downstream failure.",
            "Evidence": "Compare request identifiers and downstream traces.",
        },
    )
    _write_document(
        root / "architecture" / "payment.md",
        document_id="arch-payment",
        service="payment-service",
        environment="common",
        document_type="architecture",
        source="test-catalog",
        title="Payment service",
        sections={
            "Dependency": "Payment processing can add latency to order requests.",
            "Signals": "Use service metrics and traces to isolate slow spans.",
        },
    )
    return root


def _write_document(
    path: Path,
    *,
    document_id: str,
    service: str,
    environment: str,
    document_type: str,
    source: str,
    title: str,
    sections: dict[str, str],
) -> None:
    front_matter = "\n".join(
        (
            "---",
            f"document_id: {document_id}",
            f"service: {service}",
            f"environment: {environment}",
            f"document_type: {document_type}",
            f"source: {source}",
            "---",
            "",
            f"# {title}",
            "",
        )
    )
    body = "\n\n".join(f"## {heading}\n\n{paragraph}" for heading, paragraph in sections.items())
    path.write_text(f"{front_matter}{body}\n", encoding="utf-8")


def test_collect_documents_parses_metadata_and_chunks_by_heading(knowledge_dir: Path) -> None:
    documents = collect_documents(knowledge_dir)

    assert len(documents) == 2
    order_document = next(
        document for document in documents if document.metadata["service"] == "order-service"
    )
    chunks = chunk_markdown(order_document)

    assert order_document.source_path == "knowledge/runbooks/order-500.md"
    assert order_document.metadata["document_id"] == "rb-order-500"
    assert [chunk.section for chunk in chunks] == ["Symptoms", "Evidence"]
    assert all(chunk.content.startswith("## ") for chunk in chunks)


def test_ingestion_persists_vectors_metadata_and_generated_fts(
    database_session: Session, knowledge_dir: Path
) -> None:
    embeddings = FakeEmbeddingClient()

    result = ingest_documents(database_session, knowledge_dir, embeddings)

    assert result.created_documents == 2
    assert result.updated_documents == 0
    assert result.skipped_documents == 0
    assert result.created_chunks == 4
    documents = list(database_session.scalars(select(KnowledgeDocument)))
    chunks = list(
        database_session.scalars(select(KnowledgeChunk).order_by(KnowledgeChunk.chunk_index))
    )
    assert len(documents) == 2
    assert len(chunks) == 4
    assert all(
        chunk.embedding == [float(chunk.chunk_index + 1), 0.5, 0.25] for chunk in chunks
    )
    assert all(
        chunk.metadata_data["document_id"] in {"rb-order-500", "arch-payment"}
        for chunk in chunks
    )
    assert all("section" in chunk.metadata_data for chunk in chunks)
    assert all(
        document.content_hash == document.metadata_data["content_hash"] for document in documents
    )

    fts_match = database_session.scalar(
        select(KnowledgeChunk).where(
            KnowledgeChunk.text_search_vector.op("@@")(
                func.plainto_tsquery("english", "configuration")
            )
        )
    )
    assert fts_match is not None
    assert "configuration" in fts_match.content


def test_ingestion_is_idempotent_for_unchanged_sources(
    database_session: Session, knowledge_dir: Path
) -> None:
    embeddings = FakeEmbeddingClient()
    first_result = ingest_documents(database_session, knowledge_dir, embeddings)
    counts_before = (
        len(list(database_session.scalars(select(KnowledgeDocument)))),
        len(list(database_session.scalars(select(KnowledgeChunk)))),
    )

    second_result = ingest_documents(database_session, knowledge_dir, embeddings)
    counts_after = (
        len(list(database_session.scalars(select(KnowledgeDocument)))),
        len(list(database_session.scalars(select(KnowledgeChunk)))),
    )

    assert first_result.created_documents == 2
    assert second_result == type(second_result)(skipped_documents=2)
    assert counts_after == counts_before
    assert len(embeddings.calls) == 2


def test_ingestion_replaces_chunks_when_a_source_changes(
    database_session: Session, knowledge_dir: Path
) -> None:
    embeddings = FakeEmbeddingClient()
    first_result = ingest_documents(database_session, knowledge_dir, embeddings)
    document = database_session.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.source_path == "knowledge/runbooks/order-500.md"
        )
    )
    assert document is not None
    original_document_id = document.id
    original_hash = document.content_hash

    path = knowledge_dir / "runbooks" / "order-500.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "Check configuration errors before assuming downstream failure.",
            "Check revised configuration evidence before assuming downstream failure.",
        ),
        encoding="utf-8",
    )
    second_result = ingest_documents(database_session, knowledge_dir, embeddings)
    database_session.expire_all()
    updated_document = database_session.get(KnowledgeDocument, original_document_id)
    updated_chunks = list(
        database_session.scalars(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.document_id == original_document_id)
            .order_by(KnowledgeChunk.chunk_index)
        )
    )

    assert first_result.created_documents == 2
    assert second_result.updated_documents == 1
    assert second_result.skipped_documents == 1
    assert updated_document is not None
    assert updated_document.content_hash != original_hash
    assert any("revised configuration" in chunk.content for chunk in updated_chunks)
    assert all("Check configuration errors" not in chunk.content for chunk in updated_chunks)
    fts_match = database_session.scalar(
        select(KnowledgeChunk).where(
            KnowledgeChunk.text_search_vector.op("@@")(func.plainto_tsquery("english", "revised"))
        )
    )
    assert fts_match is not None


def test_collect_documents_rejects_missing_required_metadata(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir()
    (knowledge_root / "invalid.md").write_text(
        "---\n"
        "service: order-service\n"
        "environment: local\n"
        "document_type: runbook\n"
        "source: test\n"
        "---\n\n"
        "# Invalid\n",
        encoding="utf-8",
    )

    with pytest.raises(KnowledgeDocumentParseError, match="missing required metadata: document_id"):
        collect_documents(knowledge_root)


def test_collect_documents_rejects_invalid_metadata_value(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir()
    (knowledge_root / "invalid.md").write_text(
        "---\n"
        "document_id: 42\n"
        "service: order-service\n"
        "environment: local\n"
        "document_type: runbook\n"
        "source: test\n"
        "---\n\n"
        "# Invalid\n",
        encoding="utf-8",
    )

    with pytest.raises(KnowledgeDocumentParseError, match="metadata 'document_id'"):
        collect_documents(knowledge_root)
