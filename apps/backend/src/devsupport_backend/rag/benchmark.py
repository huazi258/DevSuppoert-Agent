"""Small real-corpus retrieval benchmark; it never changes production retrieval."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from devsupport_backend.models import KnowledgeDocument
from devsupport_backend.rag.ingest import DEFAULT_KNOWLEDGE_DIR, collect_documents
from devsupport_backend.rag.retrieval import (
    RAGService,
    RetrievalFilters,
    RetrievalResult,
    allowed_environments,
    allowed_services,
)


class RetrievalBenchmarkError(RuntimeError):
    """Raised when a benchmark precondition or suite contract is invalid."""


@dataclass(frozen=True)
class BenchmarkCase:
    id: str
    query: str
    service: str
    environment: str
    top_k: int
    required_document_id: str
    diagnostic_relevant_document_ids: tuple[str, ...] = ()


def load_suite(path: Path) -> list[BenchmarkCase]:
    """Load a non-empty benchmark suite with stable document references."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("cases"), list) or not raw["cases"]:
        raise RetrievalBenchmarkError("retrieval benchmark suite must contain at least one case")

    cases: list[BenchmarkCase] = []
    for item in raw["cases"]:
        if not isinstance(item, dict):
            raise RetrievalBenchmarkError("retrieval benchmark cases must be mappings")
        diagnostic_ids = item.get("diagnostic_relevant_document_ids", [])
        if not isinstance(diagnostic_ids, list) or not all(
            isinstance(document_id, str) and document_id for document_id in diagnostic_ids
        ):
            raise RetrievalBenchmarkError(
                "diagnostic_relevant_document_ids must be a list of document IDs"
            )
        try:
            cases.append(
                BenchmarkCase(
                    id=item["id"],
                    query=item["query"],
                    service=item["service"],
                    environment=item["environment"],
                    top_k=item["top_k"],
                    required_document_id=item["required_document_id"],
                    diagnostic_relevant_document_ids=tuple(diagnostic_ids),
                )
            )
        except KeyError as error:
            raise RetrievalBenchmarkError(
                f"retrieval benchmark case is missing {error.args[0]!r}"
            ) from error
    return cases


def ensure_corpus_fresh(
    session: Session, knowledge_dir: Path = DEFAULT_KNOWLEDGE_DIR
) -> list[dict[str, str]]:
    """Fail closed unless persisted documents exactly match the Markdown corpus."""
    parsed = collect_documents(knowledge_dir)
    expected = {item.source_path: item.content_hash for item in parsed}
    persisted_documents = list(session.scalars(select(KnowledgeDocument)))
    persisted = {item.source_path: item.content_hash for item in persisted_documents}
    if expected != persisted:
        missing = sorted(set(expected) - set(persisted))
        extra = sorted(set(persisted) - set(expected))
        drift = sorted(
            key for key in set(expected) & set(persisted) if expected[key] != persisted[key]
        )
        raise RetrievalBenchmarkError(
            f"knowledge corpus is stale: missing={missing}, extra={extra}, drift={drift}"
        )
    _stable_document_ids(persisted_documents)
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
    results: Sequence[RetrievalResult], stable_ids: dict[UUID, str]
) -> list[dict[str, object]]:
    """Deduplicate chunks by document while preserving each document's first rank."""
    seen: set[UUID] = set()
    ranked: list[dict[str, object]] = []
    for chunk in results:
        if chunk.document_id in seen:
            continue
        try:
            document_id = stable_ids[chunk.document_id]
        except KeyError as error:
            raise RetrievalBenchmarkError(
                "retrieval result references document without a stable document_id: "
                f"{chunk.document_id}"
            ) from error
        seen.add(chunk.document_id)
        ranked.append(
            {
                "rank": len(ranked) + 1,
                "document_id": document_id,
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


def run(session: Session, service: RAGService, cases: Sequence[BenchmarkCase]) -> dict[str, object]:
    """Run the real retrieval path and emit rank and metadata-eligibility diagnostics."""
    if not cases:
        raise RetrievalBenchmarkError("retrieval benchmark suite must contain at least one case")

    inventory = ensure_corpus_fresh(session)
    documents_by_stable_id = _documents_by_stable_id(
        list(session.scalars(select(KnowledgeDocument)))
    )
    stable_ids = {document.id: stable_id for stable_id, document in documents_by_stable_id.items()}
    outputs: list[dict[str, object]] = []
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
        ranks_by_document_id = {item["document_id"]: item["rank"] for item in docs}
        outputs.append(
            {
                "id": case.id,
                "query": case.query,
                "filters": {"service": case.service, "environment": case.environment},
                "top_k": case.top_k,
                "required_document_id": case.required_document_id,
                "diagnostic_documents": [
                    _eligibility_diagnostic(
                        case,
                        document_id,
                        documents_by_stable_id,
                        ranks_by_document_id,
                    )
                    for document_id in case.diagnostic_relevant_document_ids
                ],
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


def _stable_document_ids(documents: Sequence[KnowledgeDocument]) -> dict[UUID, str]:
    stable_ids: dict[UUID, str] = {}
    seen_stable_ids: set[str] = set()
    for document in documents:
        document_id = document.metadata_data.get("document_id")
        if not isinstance(document_id, str) or not document_id:
            raise RetrievalBenchmarkError(
                "persisted knowledge document is missing stable metadata_data['document_id']: "
                f"{document.source_path}"
            )
        if document_id in seen_stable_ids:
            raise RetrievalBenchmarkError(
                f"persisted knowledge documents share stable document_id: {document_id}"
            )
        stable_ids[document.id] = document_id
        seen_stable_ids.add(document_id)
    return stable_ids


def _documents_by_stable_id(
    documents: Sequence[KnowledgeDocument],
) -> dict[str, KnowledgeDocument]:
    stable_ids = _stable_document_ids(documents)
    return {stable_ids[document.id]: document for document in documents}


def _eligibility_diagnostic(
    case: BenchmarkCase,
    document_id: str,
    documents_by_stable_id: dict[str, KnowledgeDocument],
    ranks_by_document_id: dict[str, object],
) -> dict[str, object]:
    try:
        document = documents_by_stable_id[document_id]
    except KeyError as error:
        raise RetrievalBenchmarkError(
            f"case {case.id!r} references unknown diagnostic document_id: {document_id}"
        ) from error

    reasons: list[str] = []
    services = allowed_services(case.service)
    if services is not None and document.service not in services:
        reasons.append("service_mismatch")
    environments = allowed_environments(case.environment)
    if environments is not None and document.environment not in environments:
        reasons.append("environment_mismatch")
    return {
        "document_id": document_id,
        "eligible": not reasons,
        "reasons": reasons,
        "retrieved_rank": ranks_by_document_id.get(document_id),
        "requested_service": case.service,
        "document_service": document.service,
        "requested_environment": case.environment,
        "document_environment": document.environment,
    }
