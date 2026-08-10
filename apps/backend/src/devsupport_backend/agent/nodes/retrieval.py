"""Knowledge retrieval state transform using the fixed search_knowledge Tool."""

from __future__ import annotations

from uuid import UUID

from devsupport_backend.agent.state import (
    AgentStage,
    AgentState,
    EvidenceContext,
    IntakeDecision,
    ToolHistoryEntry,
)
from devsupport_backend.rag.retrieval import RAGService
from devsupport_backend.tools.schemas import (
    SearchKnowledgeInput,
    SearchKnowledgeResult,
    ToolStatus,
)
from devsupport_backend.tools.search_knowledge import search_knowledge

MAX_KNOWLEDGE_EVIDENCE_SUMMARY_CHARS = 1_000
"""Maximum source-content excerpt retained in one knowledge evidence summary."""

def retrieval_node(
    state: AgentState,
    rag_service: RAGService,
) -> AgentState:
    """Retrieve knowledge once for a ready Incident and convert results to evidence."""
    if (
        state["intake_decision"] != IntakeDecision.READY
        or state["current_stage"] != AgentStage.RETRIEVAL
    ):
        return state

    tool_input = SearchKnowledgeInput(
        query=_build_query(state),
        service=state["incident"].service,
        environment=state["incident"].environment,
    )
    tool_output = search_knowledge(tool_input, rag_service)
    tool_history_entry = ToolHistoryEntry(
        tool_name="search_knowledge",
        tool_arguments=tool_input.model_dump(mode="json"),
        status=tool_output.status,
        duration_ms=tool_output.duration_ms,
        error=tool_output.error,
    )

    if tool_output.status is not ToolStatus.SUCCESS:
        return {
            **state,
            "tool_history": [*state["tool_history"], tool_history_entry],
            "tool_call_count": state["tool_call_count"] + 1,
        }

    evidence, evidence_ids = _append_unique_evidence(state, tool_output.results)
    tool_history_entry = tool_history_entry.model_copy(update={"evidence_ids": evidence_ids})
    return {
        **state,
        "evidence": evidence,
        "tool_history": [*state["tool_history"], tool_history_entry],
        "tool_call_count": state["tool_call_count"] + 1,
        "current_stage": AgentStage.HYPOTHESIS_GENERATION,
    }


def _build_query(state: AgentState) -> str:
    """Combine only normalized Incident facts into one bounded retrieval query."""
    facts = [state["incident"].description, *state["incident"].symptoms]
    unique_facts: list[str] = []
    seen: set[str] = set()
    for fact in facts:
        key = fact.casefold()
        if key not in seen:
            unique_facts.append(fact)
            seen.add(key)

    return " ".join(unique_facts)[:2_000].rstrip()


def _append_unique_evidence(
    state: AgentState, results: list[SearchKnowledgeResult]
) -> tuple[list[EvidenceContext], list[UUID]]:
    """Retain one concise evidence record per knowledge chunk across retries."""
    evidence = [*state["evidence"]]
    evidence_ids_by_chunk = {
        str(item.data["chunk_id"]): item.id
        for item in evidence
        if item.source == "search_knowledge" and "chunk_id" in item.data
    }
    result_evidence_ids: list[UUID] = []
    for result in results:
        chunk_id = str(result.chunk_id)
        evidence_id = evidence_ids_by_chunk.get(chunk_id)
        if evidence_id is None:
            item = _knowledge_evidence(result)
            evidence.append(item)
            evidence_id = item.id
            evidence_ids_by_chunk[chunk_id] = evidence_id
        result_evidence_ids.append(evidence_id)
    return evidence, result_evidence_ids


def _knowledge_evidence(result: SearchKnowledgeResult) -> EvidenceContext:
    """Project a Tool result into bounded, citation-backed workflow evidence."""
    content_summary = result.content[:MAX_KNOWLEDGE_EVIDENCE_SUMMARY_CHARS].rstrip()
    if len(result.content) > MAX_KNOWLEDGE_EVIDENCE_SUMMARY_CHARS:
        content_summary = f"{content_summary}…"
    if not content_summary:
        content_summary = f"Knowledge result from {result.section}."
    return EvidenceContext(
        evidence_type="knowledge_retrieval",
        source="search_knowledge",
        summary=content_summary,
        data={
            "document_id": str(result.document_id),
            "chunk_id": str(result.chunk_id),
            "document_type": result.document_type,
            "service": result.service,
            "environment": result.environment,
            "source": result.source,
            "section": result.section,
            "vector_score": result.vector_score,
            "keyword_score": result.keyword_score,
            "fusion_score": result.fusion_score,
            "citation": result.citation.model_dump(mode="json"),
        },
        reference=result.citation.document_reference,
    )
