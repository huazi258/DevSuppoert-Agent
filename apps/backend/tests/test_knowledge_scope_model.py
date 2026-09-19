"""M3.1 database ownership regressions for scoped knowledge documents."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from devsupport_backend.models import (
    InvestigationTarget,
    KnowledgeChunk,
    KnowledgeDocument,
    Service,
)


def _target_and_service(session: Session, slug: str) -> tuple[InvestigationTarget, Service]:
    target = InvestigationTarget(
        name=f"{slug} target",
        slug=slug,
        environment="local",
        enabled=True,
    )
    service = Service(
        target=target,
        name="order-service",
        display_name="Order service",
        enabled=True,
    )
    session.add(target)
    session.flush()
    return target, service


def _document(**values: object) -> KnowledgeDocument:
    document_values = {
        "title": "Scoped document",
        "source_path": f"knowledge/tests/{uuid4()}.md",
        "content_hash": "a" * 64,
        "environment": "common",
        "document_type": "runbook",
        "version": "v1",
        "status": "enabled",
        "metadata_data": {},
    }
    document_values.update(values)
    return KnowledgeDocument(**document_values)


def test_shared_document_is_persisted_without_service_binding(database_session: Session) -> None:
    target, _ = _target_and_service(database_session, f"shared-{uuid4()}")
    document = _document(target_id=target.id, scope="shared")
    database_session.add(document)
    database_session.commit()

    assert document.target_id == target.id
    assert document.scope == "shared"
    assert document.service_id is None


def test_service_document_is_persisted_with_its_target_service(database_session: Session) -> None:
    target, service = _target_and_service(database_session, f"service-{uuid4()}")
    document = _document(
        target_id=target.id,
        scope="service",
        service_id=service.id,
        service="order-service",
        environment="local",
    )
    database_session.add(document)
    database_session.commit()

    assert document.target_id == target.id
    assert document.service_id == service.id
    assert document.service_record == service


def test_service_scope_without_service_id_is_rejected(database_session: Session) -> None:
    target, _ = _target_and_service(database_session, f"missing-service-{uuid4()}")
    with pytest.raises(IntegrityError, match="ck_knowledge_documents_scope_service"):
        with database_session.begin_nested():
            database_session.add(_document(target_id=target.id, scope="service"))
            database_session.flush()


def test_shared_scope_with_service_id_is_rejected(database_session: Session) -> None:
    target, service = _target_and_service(database_session, f"shared-service-{uuid4()}")
    with pytest.raises(IntegrityError, match="ck_knowledge_documents_scope_service"):
        with database_session.begin_nested():
            database_session.add(
                _document(target_id=target.id, scope="shared", service_id=service.id)
            )
            database_session.flush()


def test_shared_scope_with_legacy_service_name_is_rejected(database_session: Session) -> None:
    target, _ = _target_and_service(database_session, f"shared-name-{uuid4()}")
    with pytest.raises(IntegrityError, match="ck_knowledge_documents_scope_service"):
        with database_session.begin_nested():
            database_session.add(
                _document(target_id=target.id, scope="shared", service="order-service")
            )
            database_session.flush()


def test_cross_target_service_binding_is_rejected(database_session: Session) -> None:
    first_target, _ = _target_and_service(database_session, f"first-{uuid4()}")
    _, second_service = _target_and_service(database_session, f"second-{uuid4()}")
    with pytest.raises(IntegrityError, match="knowledge_documents_service_id_target_id_fkey"):
        with database_session.begin_nested():
            database_session.add(
                _document(
                    target_id=first_target.id,
                    scope="service",
                    service_id=second_service.id,
                )
            )
            database_session.flush()


def test_disabled_status_persists(database_session: Session) -> None:
    target, _ = _target_and_service(database_session, f"disabled-{uuid4()}")
    document = _document(target_id=target.id, scope="shared", status="disabled")
    database_session.add(document)
    database_session.commit()
    database_session.refresh(document)

    assert document.status == "disabled"


def test_chunk_reaches_its_document_scope(database_session: Session) -> None:
    target, service = _target_and_service(database_session, f"chunk-{uuid4()}")
    document = _document(
        target_id=target.id,
        scope="service",
        service_id=service.id,
        service="order-service",
        environment="local",
    )
    chunk = KnowledgeChunk(
        chunk_index=0,
        content="Scoped knowledge evidence.",
        embedding=[1.0, 0.0],
        metadata_data={},
    )
    document.chunks.append(chunk)
    database_session.add(document)
    database_session.commit()
    database_session.refresh(chunk)

    assert chunk.document.target_id == target.id
    assert chunk.document.service_id == service.id
    assert chunk.document.environment == "local"
