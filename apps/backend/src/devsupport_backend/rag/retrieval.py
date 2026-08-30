"""Hybrid PostgreSQL and pgvector retrieval for the knowledge corpus."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from devsupport_backend.models import KnowledgeChunk, KnowledgeDocument
from devsupport_backend.rag.embeddings import EmbeddingClient, EmbeddingError

RRF_K = 60
DEFAULT_CANDIDATE_MULTIPLIER = 3
SHARED_PLATFORM_SERVICE = "platform"


class RetrievalError(RuntimeError):
    """Raised when a search cannot produce a safe, interpretable result."""


@dataclass(frozen=True)
class RetrievalFilters:
    """Optional metadata constraints applied before both retrieval paths."""

    service: str | None = None
    environment: str | None = None
    document_type: str | None = None


@dataclass(frozen=True)
class Citation:
    """Stable provenance for a retrieved knowledge chunk."""

    id: str
    document_id: UUID
    chunk_id: UUID
    source: str
    section: str
    document_reference: str


@dataclass(frozen=True)
class RetrievalResult:
    """One fused result with original retrieval scores and provenance."""

    chunk_id: UUID
    document_id: UUID
    content: str
    service: str | None
    environment: str | None
    document_type: str
    source: str
    section: str
    vector_score: float | None
    keyword_score: float | None
    fusion_score: float
    citation: Citation


@dataclass
class _Candidate:
    """Internal candidate record accumulated across retrieval methods."""

    chunk: KnowledgeChunk
    document: KnowledgeDocument
    vector_score: float | None = None
    keyword_score: float | None = None
    fusion_score: float = 0.0


class RAGService:
    """Search chunk content using exact cosine similarity, FTS, and RRF."""

    def __init__(
        self,
        session: Session,
        embedding_client: EmbeddingClient,
        *,
        candidate_multiplier: int = DEFAULT_CANDIDATE_MULTIPLIER,
    ) -> None:
        if candidate_multiplier < 1:
            raise ValueError("candidate_multiplier must be at least one")
        self._session = session
        self._embedding_client = embedding_client
        self._candidate_multiplier = candidate_multiplier

    def search(
        self,
        query: str,
        filters: RetrievalFilters | None = None,
        top_k: int = 5,
    ) -> list[RetrievalResult]:
        """Return top fused candidates after applying metadata filters to both paths."""
        normalized_query = query.strip()
        if not normalized_query:
            raise RetrievalError("query must not be blank")
        if top_k < 1:
            raise RetrievalError("top_k must be at least one")

        active_filters = filters or RetrievalFilters()
        candidate_limit = top_k * self._candidate_multiplier
        dimensions = self._corpus_dimensions(active_filters)
        query_embedding = self._query_embedding(normalized_query, dimensions)
        vector_candidates = self._vector_candidates(
            query_embedding,
            active_filters,
            candidate_limit,
        )
        keyword_candidates = self._keyword_candidates(
            normalized_query,
            active_filters,
            candidate_limit,
        )
        return self._fuse(vector_candidates, keyword_candidates, top_k)

    def _corpus_dimensions(self, filters: RetrievalFilters) -> set[int]:
        statement = (
            select(KnowledgeChunk.embedding)
            .join(KnowledgeDocument)
            .where(KnowledgeChunk.embedding.is_not(None), *self._filter_clauses(filters))
        )
        dimensions = {len(vector) for vector in self._session.scalars(statement)}
        if len(dimensions) > 1:
            raise RetrievalError(
                "filtered knowledge corpus contains mixed embedding dimensions: "
                f"{sorted(dimensions)}"
            )
        return dimensions

    def _query_embedding(self, query: str, dimensions: set[int]) -> list[float] | None:
        if not dimensions:
            return None
        try:
            vectors = self._embedding_client.embed([query])
        except EmbeddingError as error:
            raise RetrievalError(f"query embedding failed: {error}") from error
        if len(vectors) != 1 or not vectors[0]:
            raise RetrievalError("embedding client must return exactly one non-empty query vector")
        query_vector = vectors[0]
        corpus_dimension = dimensions.pop()
        if len(query_vector) != corpus_dimension:
            raise RetrievalError(
                "query embedding dimension does not match filtered corpus: "
                f"query={len(query_vector)}, corpus={corpus_dimension}"
            )
        return query_vector

    def _vector_candidates(
        self,
        query_embedding: list[float] | None,
        filters: RetrievalFilters,
        candidate_limit: int,
    ) -> list[tuple[KnowledgeChunk, KnowledgeDocument, float]]:
        if query_embedding is None:
            return []
        distance = KnowledgeChunk.embedding.cosine_distance(query_embedding)
        statement = (
            select(KnowledgeChunk, KnowledgeDocument, (1 - distance).label("vector_score"))
            .join(KnowledgeDocument)
            .where(KnowledgeChunk.embedding.is_not(None), *self._filter_clauses(filters))
            .order_by(distance, KnowledgeChunk.id)
            .limit(candidate_limit)
        )
        return [
            (chunk, document, float(vector_score))
            for chunk, document, vector_score in self._session.execute(statement)
        ]

    def _keyword_candidates(
        self,
        query: str,
        filters: RetrievalFilters,
        candidate_limit: int,
    ) -> list[tuple[KnowledgeChunk, KnowledgeDocument, float]]:
        tsquery = func.plainto_tsquery("english", query)
        keyword_score = func.ts_rank_cd(KnowledgeChunk.text_search_vector, tsquery)
        statement = (
            select(KnowledgeChunk, KnowledgeDocument, keyword_score.label("keyword_score"))
            .join(KnowledgeDocument)
            .where(
                KnowledgeChunk.text_search_vector.op("@@")(tsquery),
                *self._filter_clauses(filters),
            )
            .order_by(keyword_score.desc(), KnowledgeChunk.id)
            .limit(candidate_limit)
        )
        return [
            (chunk, document, float(score))
            for chunk, document, score in self._session.execute(statement)
        ]

    @staticmethod
    def _filter_clauses(filters: RetrievalFilters) -> list[object]:
        clauses: list[object] = []
        if services := allowed_services(filters.service):
            clauses.append(KnowledgeDocument.service.in_(services))
        if environments := allowed_environments(filters.environment):
            clauses.append(KnowledgeDocument.environment.in_(environments))
        if filters.document_type:
            clauses.append(KnowledgeDocument.document_type == filters.document_type)
        return clauses

    @staticmethod
    def _fuse(
        vector_candidates: Sequence[tuple[KnowledgeChunk, KnowledgeDocument, float]],
        keyword_candidates: Sequence[tuple[KnowledgeChunk, KnowledgeDocument, float]],
        top_k: int,
    ) -> list[RetrievalResult]:
        candidates: dict[UUID, _Candidate] = {}
        for rank, (chunk, document, score) in enumerate(vector_candidates, start=1):
            candidate = candidates.setdefault(chunk.id, _Candidate(chunk=chunk, document=document))
            candidate.vector_score = score
            candidate.fusion_score += 1 / (RRF_K + rank)
        for rank, (chunk, document, score) in enumerate(keyword_candidates, start=1):
            candidate = candidates.setdefault(chunk.id, _Candidate(chunk=chunk, document=document))
            candidate.keyword_score = score
            candidate.fusion_score += 1 / (RRF_K + rank)

        ordered_candidates = sorted(
            candidates.values(),
            key=lambda candidate: (
                -candidate.fusion_score,
                -(candidate.vector_score or 0.0),
                -(candidate.keyword_score or 0.0),
                str(candidate.chunk.id),
            ),
        )
        return [RAGService._result(candidate) for candidate in ordered_candidates[:top_k]]

    @staticmethod
    def _result(candidate: _Candidate) -> RetrievalResult:
        metadata = candidate.chunk.metadata_data
        try:
            source = str(metadata["source"])
            section = str(metadata["section"])
            document_reference = str(metadata["document_id"])
        except KeyError as error:
            raise RetrievalError(
                f"knowledge chunk {candidate.chunk.id} is missing citation metadata "
                f"{error.args[0]!r}"
            ) from error
        citation = Citation(
            id=f"knowledge:{candidate.document.id}:{candidate.chunk.id}",
            document_id=candidate.document.id,
            chunk_id=candidate.chunk.id,
            source=source,
            section=section,
            document_reference=document_reference,
        )
        return RetrievalResult(
            chunk_id=candidate.chunk.id,
            document_id=candidate.document.id,
            content=candidate.chunk.content,
            service=candidate.document.service,
            environment=candidate.document.environment,
            document_type=candidate.document.document_type,
            source=source,
            section=section,
            vector_score=candidate.vector_score,
            keyword_score=candidate.keyword_score,
            fusion_score=candidate.fusion_score,
            citation=citation,
        )


def allowed_services(service: str | None) -> frozenset[str] | None:
    """Return a service's own scope plus shared platform knowledge."""
    if not service:
        return None
    if service == SHARED_PLATFORM_SERVICE:
        return frozenset({SHARED_PLATFORM_SERVICE})
    return frozenset({service, SHARED_PLATFORM_SERVICE})


def allowed_environments(environment: str | None) -> frozenset[str] | None:
    """Return an environment's own scope plus common knowledge."""
    if not environment:
        return None
    if environment == "common":
        return frozenset({"common"})
    return frozenset({environment, "common"})
