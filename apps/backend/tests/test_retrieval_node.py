"""Tests for the stateful boundary around the fixed search_knowledge Tool."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import devsupport_backend.agent.nodes.retrieval as retrieval_module
from devsupport_backend.agent.nodes.retrieval import retrieval_node
from devsupport_backend.agent.state import (
    AgentStage,
    EvidenceContext,
    IntakeDecision,
    create_initial_agent_state,
)
from devsupport_backend.models import Incident
from devsupport_backend.tools.schemas import (
    CitationOutput,
    SearchKnowledgeInput,
    SearchKnowledgeOutput,
    SearchKnowledgeResult,
    ToolError,
    ToolStatus,
)


def build_ready_state() -> dict[str, object]:
    """Create an Intake-approved state ready to run one knowledge search."""
    started_at = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
    incident = Incident(
        id=uuid4(),
        service="order-service",
        environment="local",
        description="POST /orders returns 500 after deployment",
        time_range_start=started_at,
        time_range_end=started_at + timedelta(minutes=5),
    )
    state = create_initial_agent_state(incident, symptoms=["HTTP 500", "POST /orders returns 500"])
    state["intake_decision"] = IntakeDecision.READY
    state["current_stage"] = AgentStage.RETRIEVAL
    return state


def search_result(
    *, chunk_id: UUID | None = None, content: str = "Configuration runbook excerpt."
) -> SearchKnowledgeResult:
    """Build a realistic, traceable result returned by the existing Tool contract."""
    document_id = uuid4()
    result_chunk_id = chunk_id or uuid4()
    return SearchKnowledgeResult(
        chunk_id=result_chunk_id,
        document_id=document_id,
        content=content,
        service="order-service",
        environment="local",
        document_type="runbook",
        source="knowledge/runbooks/order-service-500.md",
        section="Investigate errors",
        vector_score=0.9,
        keyword_score=0.7,
        fusion_score=0.03,
        citation=CitationOutput(
            id=f"citation:{result_chunk_id}",
            document_id=document_id,
            chunk_id=result_chunk_id,
            source="knowledge/runbooks/order-service-500.md",
            section="Investigate errors",
            document_reference=f"knowledge/runbooks/order-service-500.md#chunk-{result_chunk_id}",
        ),
    )


def test_successful_retrieval_adds_cited_evidence_and_advances_stage(monkeypatch) -> None:
    state = build_ready_state()
    result = search_result(content="x" * 1_100)
    captured_inputs: list[SearchKnowledgeInput] = []

    def fake_search(tool_input: SearchKnowledgeInput, _: object) -> SearchKnowledgeOutput:
        captured_inputs.append(tool_input)
        return SearchKnowledgeOutput(status=ToolStatus.SUCCESS, duration_ms=3.5, results=[result])

    monkeypatch.setattr(retrieval_module, "search_knowledge", fake_search)
    updated = retrieval_node(state, object())  # type: ignore[arg-type]

    assert captured_inputs == [
        SearchKnowledgeInput(
            query="POST /orders returns 500 after deployment HTTP 500 POST /orders returns 500",
            service="order-service",
            environment="local",
        )
    ]
    assert updated["current_stage"] is AgentStage.HYPOTHESIS_GENERATION
    assert updated["tool_call_count"] == 1
    assert updated["investigation_round"] == 0
    assert len(updated["evidence"]) == 1
    evidence = updated["evidence"][0]
    assert evidence.summary == f"{'x' * 1_000}…"
    assert evidence.reference == result.citation.document_reference
    assert evidence.data["chunk_id"] == str(result.chunk_id)
    assert evidence.data["document_id"] == str(result.document_id)
    assert evidence.data["citation"] == result.citation.model_dump(mode="json")
    assert updated["tool_history"][0].evidence_ids == [evidence.id]


def test_retrieval_failure_records_tool_history_without_evidence(monkeypatch) -> None:
    state = build_ready_state()

    def failing_search(_: SearchKnowledgeInput, __: object) -> SearchKnowledgeOutput:
        return SearchKnowledgeOutput(
            status=ToolStatus.FAILURE,
            error=ToolError(code="retrieval_error", message="Embedding dimensions do not match"),
            duration_ms=1.0,
        )

    monkeypatch.setattr(retrieval_module, "search_knowledge", failing_search)
    updated = retrieval_node(state, object())  # type: ignore[arg-type]

    assert updated["current_stage"] is AgentStage.RETRIEVAL
    assert updated["evidence"] == []
    assert updated["tool_call_count"] == 1
    assert updated["tool_history"][0].status is ToolStatus.FAILURE
    assert updated["tool_history"][0].error is not None


def test_repeated_retrieval_does_not_duplicate_existing_chunk_evidence(monkeypatch) -> None:
    state = build_ready_state()
    result = search_result()

    def fake_search(_: SearchKnowledgeInput, __: object) -> SearchKnowledgeOutput:
        return SearchKnowledgeOutput(status=ToolStatus.SUCCESS, duration_ms=1.0, results=[result])

    monkeypatch.setattr(retrieval_module, "search_knowledge", fake_search)
    first = retrieval_node(state, object())  # type: ignore[arg-type]
    repeated_state = {**first, "current_stage": AgentStage.RETRIEVAL}
    second = retrieval_node(repeated_state, object())  # type: ignore[arg-type]

    assert len(second["evidence"]) == 1
    assert second["tool_call_count"] == 2
    assert len(second["tool_history"]) == 2
    assert second["tool_history"][1].evidence_ids == [second["evidence"][0].id]


def test_retrieval_does_not_call_tool_when_intake_or_stage_is_not_ready(monkeypatch) -> None:
    state = build_ready_state()
    calls = 0

    def fake_search(_: SearchKnowledgeInput, __: object) -> SearchKnowledgeOutput:
        nonlocal calls
        calls += 1
        return SearchKnowledgeOutput(status=ToolStatus.SUCCESS)

    monkeypatch.setattr(retrieval_module, "search_knowledge", fake_search)
    state["intake_decision"] = IntakeDecision.NEEDS_INFORMATION
    skipped_for_intake = retrieval_node(state, object())  # type: ignore[arg-type]
    state["intake_decision"] = IntakeDecision.READY
    state["current_stage"] = AgentStage.INTAKE
    skipped_for_stage = retrieval_node(state, object())  # type: ignore[arg-type]

    assert calls == 0
    assert skipped_for_intake is state
    assert skipped_for_stage is state


def test_retrieval_leaves_unowned_investigation_fields_unchanged(monkeypatch) -> None:
    state = build_ready_state()
    existing_evidence = EvidenceContext(
        evidence_type="incident_fact",
        source="intake",
        summary="The caller reported a 500 response.",
    )
    state["evidence"].append(existing_evidence)
    state["investigation_round"] = 4

    def fake_search(_: SearchKnowledgeInput, __: object) -> SearchKnowledgeOutput:
        return SearchKnowledgeOutput(status=ToolStatus.SUCCESS, results=[])

    monkeypatch.setattr(retrieval_module, "search_knowledge", fake_search)
    updated = retrieval_node(state, object())  # type: ignore[arg-type]

    assert updated["hypotheses"] == []
    assert updated["proposed_action"] is None
    assert updated["final_conclusion"] is None
    assert updated["investigation_round"] == 4
    assert updated["evidence"] == [existing_evidence]
