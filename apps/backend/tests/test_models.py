from devsupport_backend.models import (
    Action,
    Approval,
    Base,
    Evidence,
    Incident,
    KnowledgeChunk,
    KnowledgeDocument,
    Report,
    ToolCall,
    Verification,
)


def test_initial_domain_and_knowledge_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == {
        "actions",
        "approvals",
        "evidence",
        "hypotheses",
        "incidents",
        "knowledge_chunks",
        "knowledge_documents",
        "reports",
        "tool_calls",
        "verifications",
    }


def test_knowledge_chunk_is_related_to_its_document() -> None:
    document = KnowledgeDocument(
        title="Order service runbook",
        source_path="knowledge/runbooks/order-service.md",
        document_type="runbook",
    )
    chunk = KnowledgeChunk(chunk_index=0, content="Investigate order-service failures.")
    document.chunks.append(chunk)

    assert chunk.document is document


def test_schema_models_include_required_v0_fields_without_fixed_vector_dimensions() -> None:
    incident_columns = Incident.__table__.c
    evidence_columns = Evidence.__table__.c
    tool_call_columns = ToolCall.__table__.c
    approval_columns = Approval.__table__.c
    action_columns = Action.__table__.c
    verification_columns = Verification.__table__.c
    report_columns = Report.__table__.c
    knowledge_document_columns = KnowledgeDocument.__table__.c
    chunk_embedding = KnowledgeChunk.__table__.c.embedding

    assert {"time_range_start", "time_range_end", "thread_id"} <= set(incident_columns.keys())
    assert incident_columns.thread_id.nullable
    assert {"hypothesis_id", "evidence_type", "data"} <= set(evidence_columns.keys())
    assert {"result", "error", "duration_ms"} <= set(tool_call_columns.keys())
    assert {"action_id", "status"} <= set(approval_columns.keys())
    assert {"parameters", "executed_at"} <= set(action_columns.keys())
    assert {"action_id", "details"} <= set(verification_columns.keys())
    assert {"content", "root_cause"} <= set(report_columns.keys())
    assert "content_hash" in knowledge_document_columns
    assert chunk_embedding.type.dim is None
