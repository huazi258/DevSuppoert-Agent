"""Deterministic grounding predicates shared by hypothesis and conclusion nodes."""

from __future__ import annotations

from uuid import UUID

from devsupport_backend.agent.state import (
    AgentState,
    EvidenceContext,
    HypothesisContext,
    HypothesisStatus,
)

_RUNTIME_EVIDENCE_SOURCES = frozenset(
    {"query_logs", "query_metrics", "query_traces", "get_deployment_history"}
)


def current_round_evidence(state: AgentState) -> dict[UUID, EvidenceContext]:
    """Return evidence belonging to this persisted V2 InvestigationRound only."""
    round_id = state["round_id"]
    return {
        evidence.id: evidence
        for evidence in state["evidence"]
        if evidence.round_id == round_id
    }


def is_knowledge_evidence(evidence: EvidenceContext) -> bool:
    """Identify the citation-bearing knowledge path without trusting display text."""
    return (
        evidence.source == "search_knowledge"
        and evidence.evidence_type == "knowledge_retrieval"
    )


def is_runtime_evidence(evidence: EvidenceContext) -> bool:
    """Only Tool-derived runtime facts can establish an incident root cause."""
    return evidence.source in _RUNTIME_EVIDENCE_SOURCES


def eligible_conclusion_hypothesis(state: AgentState) -> HypothesisContext | None:
    """Select one confirmed hypothesis that satisfies every deterministic conclusion guard."""
    evidence_by_id = current_round_evidence(state)
    for hypothesis in state["hypotheses"]:
        if hypothesis.status is not HypothesisStatus.CONFIRMED:
            continue
        supporting_ids = set(hypothesis.supporting_evidence_ids)
        contradicting_ids = set(hypothesis.contradicting_evidence_ids)
        referenced_ids = supporting_ids | contradicting_ids
        if (
            not supporting_ids
            or contradicting_ids
            or not referenced_ids.issubset(evidence_by_id)
        ):
            continue
        referenced_evidence = [evidence_by_id[evidence_id] for evidence_id in referenced_ids]
        if any(
            is_knowledge_evidence(evidence) and evidence.citation is None
            for evidence in referenced_evidence
        ):
            continue
        if not any(
            is_runtime_evidence(evidence_by_id[evidence_id])
            for evidence_id in supporting_ids
        ):
            continue
        return hypothesis
    return None


def validate_confirmed_hypothesis(
    hypothesis: HypothesisContext, evidence_by_id: dict[UUID, EvidenceContext]
) -> None:
    """Reject LLM attempts to confirm a hypothesis without grounded runtime support."""
    if hypothesis.status is not HypothesisStatus.CONFIRMED:
        return
    supporting_ids = set(hypothesis.supporting_evidence_ids)
    contradicting_ids = set(hypothesis.contradicting_evidence_ids)
    referenced_ids = supporting_ids | contradicting_ids
    if not supporting_ids:
        raise ValueError("a confirmed hypothesis requires supporting evidence")
    if contradicting_ids:
        raise ValueError("a confirmed hypothesis cannot ignore contradicting evidence")
    if not referenced_ids.issubset(evidence_by_id):
        raise ValueError("a confirmed hypothesis referenced evidence outside the current round")
    if not any(is_runtime_evidence(evidence_by_id[item]) for item in supporting_ids):
        raise ValueError("a confirmed hypothesis requires runtime supporting evidence")
