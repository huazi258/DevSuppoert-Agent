"""Structured tool adapter that delegates knowledge search to RAGService."""

from time import perf_counter

from devsupport_backend.rag.retrieval import RAGService, RetrievalError, RetrievalFilters
from devsupport_backend.tools.schemas import (
    CitationOutput,
    SearchKnowledgeInput,
    SearchKnowledgeOutput,
    SearchKnowledgeResult,
    ToolError,
    ToolStatus,
)


def search_knowledge(
    tool_input: SearchKnowledgeInput,
    rag_service: RAGService,
) -> SearchKnowledgeOutput:
    """Execute the registered knowledge tool without duplicating retrieval logic."""
    started_at = perf_counter()
    try:
        results = rag_service.search(
            tool_input.query,
            filters=RetrievalFilters(
                service=tool_input.service,
                environment=tool_input.environment,
                document_type=tool_input.document_type,
            ),
            top_k=tool_input.top_k,
        )
    except RetrievalError as error:
        return SearchKnowledgeOutput(
            status=ToolStatus.FAILURE,
            error=ToolError(code="retrieval_error", message=str(error)),
            duration_ms=_duration_ms(started_at),
        )

    return SearchKnowledgeOutput(
        status=ToolStatus.SUCCESS,
        duration_ms=_duration_ms(started_at),
        results=[
            SearchKnowledgeResult(
                chunk_id=result.chunk_id,
                document_id=result.document_id,
                content=result.content,
                service=result.service,
                environment=result.environment,
                document_type=result.document_type,
                source=result.source,
                section=result.section,
                vector_score=result.vector_score,
                keyword_score=result.keyword_score,
                fusion_score=result.fusion_score,
                citation=CitationOutput(
                    id=result.citation.id,
                    document_id=result.citation.document_id,
                    chunk_id=result.citation.chunk_id,
                    source=result.citation.source,
                    section=result.citation.section,
                    document_reference=result.citation.document_reference,
                ),
            )
            for result in results
        ],
    )


def _duration_ms(started_at: float) -> float:
    """Return a non-negative elapsed duration suitable for a later ToolCall audit."""
    return max(0.0, round((perf_counter() - started_at) * 1_000, 2))
