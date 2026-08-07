from devsupport_backend.models import Base, KnowledgeChunk, KnowledgeDocument


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
