"""Command-line knowledge ingestion pipeline."""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from devsupport_backend.config import settings
from devsupport_backend.database import SessionLocal
from devsupport_backend.models import KnowledgeChunk, KnowledgeDocument
from devsupport_backend.rag.embeddings import (
    EmbeddingClient,
    EmbeddingError,
    OpenAICompatibleEmbeddingClient,
)
from devsupport_backend.rag.markdown import (
    Chunk,
    KnowledgeDocumentParseError,
    ParsedKnowledgeDocument,
    chunk_markdown,
    parse_markdown,
)

DEFAULT_KNOWLEDGE_DIR = Path(__file__).resolve().parents[5] / "knowledge"


@dataclass(frozen=True)
class IngestionResult:
    """Counts produced by one all-or-nothing ingestion run."""

    created_documents: int = 0
    updated_documents: int = 0
    skipped_documents: int = 0
    created_chunks: int = 0


def collect_documents(knowledge_dir: Path) -> list[ParsedKnowledgeDocument]:
    """Parse every Markdown file before any database writes begin."""
    if not knowledge_dir.is_dir():
        raise KnowledgeDocumentParseError(f"knowledge directory does not exist: {knowledge_dir}")
    paths = sorted(knowledge_dir.rglob("*.md"))
    if not paths:
        raise KnowledgeDocumentParseError(f"no Markdown documents found in {knowledge_dir}")
    return [parse_markdown(path, knowledge_dir) for path in paths]


def ingest_documents(
    session: Session,
    knowledge_dir: Path,
    embedding_client: EmbeddingClient,
) -> IngestionResult:
    """Persist parsed knowledge with embeddings, replacing chunks when content changes."""
    documents = collect_documents(knowledge_dir)
    result = IngestionResult()
    try:
        for parsed_document in documents:
            result = _ingest_document(session, parsed_document, embedding_client, result)
        session.commit()
    except Exception:
        session.rollback()
        raise
    return result


def _ingest_document(
    session: Session,
    parsed_document: ParsedKnowledgeDocument,
    embedding_client: EmbeddingClient,
    result: IngestionResult,
) -> IngestionResult:
    existing = session.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.source_path == parsed_document.source_path
        )
    )
    if existing is not None and existing.content_hash == parsed_document.content_hash:
        return IngestionResult(
            created_documents=result.created_documents,
            updated_documents=result.updated_documents,
            skipped_documents=result.skipped_documents + 1,
            created_chunks=result.created_chunks,
        )

    chunks = chunk_markdown(parsed_document)
    vectors = embedding_client.embed([chunk.content for chunk in chunks])
    if len(vectors) != len(chunks):
        raise EmbeddingError(
            f"{parsed_document.source_path}: embedding client returned {len(vectors)} vectors "
            f"for {len(chunks)} chunks"
        )

    if existing is None:
        document = KnowledgeDocument()
        session.add(document)
        created_documents = result.created_documents + 1
        updated_documents = result.updated_documents
    else:
        document = existing
        document.chunks.clear()
        session.flush()
        created_documents = result.created_documents
        updated_documents = result.updated_documents + 1

    document.title = parsed_document.title
    document.source_path = parsed_document.source_path
    document.content_hash = parsed_document.content_hash
    document.document_type = parsed_document.metadata["document_type"]
    document.service = parsed_document.metadata["service"]
    document.environment = parsed_document.metadata["environment"]
    document.metadata_data = {
        **parsed_document.metadata,
        "content_hash": parsed_document.content_hash,
    }
    document.chunks.extend(_database_chunks(chunks, parsed_document.metadata, vectors))

    return IngestionResult(
        created_documents=created_documents,
        updated_documents=updated_documents,
        skipped_documents=result.skipped_documents,
        created_chunks=result.created_chunks + len(chunks),
    )


def _database_chunks(
    chunks: Iterable[Chunk], metadata: dict[str, str], vectors: list[list[float]]
) -> list[KnowledgeChunk]:
    """Attach stable source and section metadata to every persisted chunk."""
    return [
        KnowledgeChunk(
            chunk_index=chunk.index,
            content=chunk.content,
            metadata_data={**metadata, "section": chunk.section, "chunk_index": chunk.index},
            embedding=vector,
        )
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]


def main() -> None:
    """Import the repository knowledge corpus using the configured embedding provider."""
    parser = argparse.ArgumentParser(
        description="Ingest DevSupport Markdown knowledge into PostgreSQL"
    )
    parser.add_argument("--knowledge-dir", type=Path, default=DEFAULT_KNOWLEDGE_DIR)
    arguments = parser.parse_args()

    try:
        embedding_client = OpenAICompatibleEmbeddingClient.from_settings(settings)
        with SessionLocal() as session:
            result = ingest_documents(session, arguments.knowledge_dir.resolve(), embedding_client)
    except (EmbeddingError, KnowledgeDocumentParseError) as error:
        parser.error(str(error))
    print(
        "Knowledge ingestion complete: "
        f"created_documents={result.created_documents} "
        f"updated_documents={result.updated_documents} "
        f"skipped_documents={result.skipped_documents} "
        f"created_chunks={result.created_chunks}"
    )


if __name__ == "__main__":
    main()
