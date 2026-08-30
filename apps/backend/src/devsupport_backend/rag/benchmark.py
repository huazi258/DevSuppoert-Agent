"""Small real-corpus retrieval benchmark; it never changes production retrieval."""
# ruff: noqa: E501, E701, E702

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from devsupport_backend.models import KnowledgeDocument
from devsupport_backend.rag.ingest import DEFAULT_KNOWLEDGE_DIR, collect_documents
from devsupport_backend.rag.retrieval import RAGService, RetrievalFilters, RetrievalResult


@dataclass(frozen=True)
class BenchmarkCase:
    id: str
    query: str
    service: str
    environment: str
    top_k: int
    required_document_id: str


def load_suite(path: Path) -> list[BenchmarkCase]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [BenchmarkCase(**item) for item in raw["cases"]]


def ensure_corpus_fresh(
    session: Session, knowledge_dir: Path = DEFAULT_KNOWLEDGE_DIR
) -> list[dict[str, str]]:
    parsed = collect_documents(knowledge_dir)
    expected = {item.source_path: item.content_hash for item in parsed}
    persisted = {
        item.source_path: item.content_hash for item in session.scalars(select(KnowledgeDocument))
    }
    if expected != persisted:
        missing = sorted(set(expected) - set(persisted))
        extra = sorted(set(persisted) - set(expected))
        drift = sorted(
            key for key in set(expected) & set(persisted) if expected[key] != persisted[key]
        )
        raise RuntimeError(
            f"knowledge corpus is stale: missing={missing}, extra={extra}, drift={drift}"
        )
    return [
        {
            "document_id": item.metadata["document_id"],
            "service": item.metadata["service"],
            "environment": item.metadata["environment"],
            "document_type": item.metadata["document_type"],
            "source": item.metadata["source"],
        }
        for item in parsed
    ]


def document_ranking(
    results: list[RetrievalResult], stable_ids: dict[UUID, str]
) -> list[dict[str, object]]:
    seen: set[UUID] = set()
    ranked = []
    for chunk in results:
        if chunk.document_id in seen:
            continue
        seen.add(chunk.document_id)
        ranked.append(
            {
                "rank": len(ranked) + 1,
                "document_id": stable_ids[chunk.document_id],
                "section": chunk.section,
                "source": chunk.source,
                "vector_score": chunk.vector_score,
                "keyword_score": chunk.keyword_score,
                "fusion_score": chunk.fusion_score,
                "citation": {
                    "id": chunk.citation.id,
                    "document_id": str(chunk.citation.document_id),
                    "chunk_id": str(chunk.citation.chunk_id),
                    "source": chunk.citation.source,
                    "section": chunk.citation.section,
                    "document_reference": chunk.citation.document_reference,
                },
            }
        )
    return ranked


def run(session: Session, service: RAGService, cases: list[BenchmarkCase]) -> dict[str, object]:
    inventory = ensure_corpus_fresh(session)
    outputs = []
    stable_ids = {
        item.id: item.metadata_data["document_id"]
        for item in session.scalars(select(KnowledgeDocument))
    }
    for case in cases:
        docs = document_ranking(
            service.search(
                case.query,
                RetrievalFilters(service=case.service, environment=case.environment),
                case.top_k,
            ),
            stable_ids,
        )
        rank = next(
            (item["rank"] for item in docs if item["document_id"] == case.required_document_id),
            None,
        )
        outputs.append(
            {
                "id": case.id,
                "query": case.query,
                "filters": {"service": case.service, "environment": case.environment},
                "top_k": case.top_k,
                "required_document_id": case.required_document_id,
                "documents": docs,
                "passed": rank is not None,
                "required_rank": rank,
                "reciprocal_rank": 0 if rank is None else 1 / rank,
            }
        )
    return {
        "inventory": inventory,
        "cases": outputs,
        "aggregate": {
            "passed": sum(item["passed"] for item in outputs),
            "total": len(outputs),
            "hit_at_k": sum(item["passed"] for item in outputs) / len(outputs),
            "mrr": sum(item["reciprocal_rank"] for item in outputs) / len(outputs),
        },
    }
