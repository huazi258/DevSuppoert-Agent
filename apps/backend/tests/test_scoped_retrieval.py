"""M3.2 scope-first regressions for formal V2 hybrid retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from devsupport_backend.models import (
    MIGRATION_COMPATIBILITY_TARGET_SLUG,
    InvestigationTarget,
    KnowledgeChunk,
    KnowledgeDocument,
    Service,
)
from devsupport_backend.rag.retrieval import KnowledgeScope, RAGService


class _EmbeddingClient:
    def embed(self, _: list[str]) -> list[list[float]]:
        return [[1.0, 0.0]]


@dataclass(frozen=True)
class _TargetServices:
    target: InvestigationTarget
    order: Service
    payment: Service


def _target_services(session: Session, slug: str) -> _TargetServices:
    target = InvestigationTarget(
        name=f"{slug} target",
        slug=slug,
        environment="local",
        enabled=True,
    )
    order = Service(
        target=target,
        name="order-service",
        display_name="Order service",
        enabled=True,
    )
    payment = Service(
        target=target,
        name="payment-service",
        display_name="Payment service",
        enabled=True,
    )
    session.add(target)
    session.flush()
    return _TargetServices(target=target, order=order, payment=payment)


def _add_document(
    session: Session,
    *,
    target_id: UUID,
    scope: str,
    service: Service | None,
    environment: str,
    status: str = "enabled",
    content: str = "scope evidence",
    embedding: list[float] | None = None,
) -> KnowledgeChunk:
    document = KnowledgeDocument(
        title="Scoped retrieval document",
        source_path=f"knowledge/scoped/{uuid4()}.md",
        content_hash="a" * 64,
        target_id=target_id,
        scope=scope,
        service_id=service.id if service else None,
        service=service.name if service else None,
        environment=environment,
        document_type="runbook",
        version="v1",
        status=status,
        metadata_data={"document_id": str(uuid4()), "source": "scoped-retrieval-test"},
    )
    chunk = KnowledgeChunk(
        chunk_index=0,
        content=content,
        embedding=embedding or [0.8, 0.2],
        metadata_data={
            "document_id": document.metadata_data["document_id"],
            "source": "scoped-retrieval-test",
            "section": "Scope",
        },
    )
    document.chunks.append(chunk)
    session.add(document)
    session.flush()
    return chunk


def _scope(services: _TargetServices) -> KnowledgeScope:
    return KnowledgeScope(
        target_id=services.target.id,
        service_id=services.order.id,
        environment="local",
    )


def test_scoped_retrieval_includes_only_current_target_service_and_common_documents(
    database_session: Session,
) -> None:
    current = _target_services(database_session, f"current-{uuid4()}")
    other = _target_services(database_session, f"other-{uuid4()}")
    shared = _add_document(
        database_session,
        target_id=current.target.id,
        scope="shared",
        service=None,
        environment="common",
    )
    service_document = _add_document(
        database_session,
        target_id=current.target.id,
        scope="service",
        service=current.order,
        environment="local",
    )
    other_service = _add_document(
        database_session,
        target_id=current.target.id,
        scope="service",
        service=current.payment,
        environment="common",
        content="scope evidence scope evidence scope evidence",
        embedding=[1.0, 0.0],
    )
    other_target = _add_document(
        database_session,
        target_id=other.target.id,
        scope="shared",
        service=None,
        environment="common",
        content="scope evidence scope evidence scope evidence",
        embedding=[1.0, 0.0],
    )
    disabled = _add_document(
        database_session,
        target_id=current.target.id,
        scope="shared",
        service=None,
        environment="common",
        status="disabled",
    )
    other_environment = _add_document(
        database_session,
        target_id=current.target.id,
        scope="shared",
        service=None,
        environment="staging",
    )
    database_session.commit()

    results = RAGService(database_session, _EmbeddingClient()).search_scoped(
        "scope evidence", scope=_scope(current), top_k=10
    )

    assert {result.chunk_id for result in results} == {shared.id, service_document.id}
    assert {other_service.id, other_target.id, disabled.id, other_environment.id}.isdisjoint(
        result.chunk_id for result in results
    )


def test_fts_and_vector_candidates_share_the_same_scope_predicate(
    database_session: Session,
) -> None:
    current = _target_services(database_session, f"predicate-current-{uuid4()}")
    other = _target_services(database_session, f"predicate-other-{uuid4()}")
    allowed_shared = _add_document(
        database_session,
        target_id=current.target.id,
        scope="shared",
        service=None,
        environment="common",
        content="scope predicate evidence",
        embedding=[0.9, 0.1],
    )
    allowed_service = _add_document(
        database_session,
        target_id=current.target.id,
        scope="service",
        service=current.order,
        environment="local",
        content="scope predicate evidence",
        embedding=[0.8, 0.2],
    )
    _add_document(
        database_session,
        target_id=other.target.id,
        scope="shared",
        service=None,
        environment="common",
        content="scope predicate evidence",
        embedding=[1.0, 0.0],
    )
    database_session.commit()
    service = RAGService(database_session, _EmbeddingClient())
    scope = _scope(current)

    vector_ids = {
        chunk.id
        for chunk, _, _ in service._vector_candidates_for_scope(  # noqa: SLF001
            [1.0, 0.0], scope, None, 10
        )
    }
    keyword_ids = {
        chunk.id
        for chunk, _, _ in service._keyword_candidates_for_scope(  # noqa: SLF001
            "scope predicate evidence", scope, None, 10
        )
    }

    assert vector_ids == {allowed_shared.id, allowed_service.id}
    assert keyword_ids == {allowed_shared.id, allowed_service.id}


def test_high_scoring_compatibility_document_cannot_contaminate_a_formal_target(
    database_session: Session,
) -> None:
    current = _target_services(database_session, f"normal-{uuid4()}")
    compatibility_target = database_session.scalar(
        select(InvestigationTarget).where(
            InvestigationTarget.slug == MIGRATION_COMPATIBILITY_TARGET_SLUG
        )
    )
    assert compatibility_target is not None
    allowed = _add_document(
        database_session,
        target_id=current.target.id,
        scope="shared",
        service=None,
        environment="common",
        content="authorized scope evidence",
        embedding=[0.6, 0.4],
    )
    _add_document(
        database_session,
        target_id=compatibility_target.id,
        scope="shared",
        service=None,
        environment="common",
        content="authorized scope evidence authorized scope evidence authorized scope evidence",
        embedding=[1.0, 0.0],
    )
    database_session.commit()

    results = RAGService(database_session, _EmbeddingClient()).search_scoped(
        "authorized scope evidence", scope=_scope(current), top_k=1
    )

    assert [result.chunk_id for result in results] == [allowed.id]
