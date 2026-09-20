"""M5.4 API regressions for structured, atomic Markdown knowledge management."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from devsupport_backend.database import engine, get_session
from devsupport_backend.main import app
from devsupport_backend.models import (
    InvestigationTarget,
    KnowledgeChunk,
    KnowledgeDocument,
    Service,
)
from devsupport_backend.rag.embeddings import EmbeddingError
from devsupport_backend.rag.retrieval import KnowledgeScope, RAGService
from devsupport_backend.routers.knowledge import get_embedding_client, get_target_config_registry
from devsupport_backend.target_config import (
    AdapterType,
    CapabilityConfig,
    InvestigationTargetConfig,
    TargetConfigRegistry,
    TargetServiceConfig,
)


class FakeEmbeddingClient:
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


class FailingEmbeddingClient:
    def embed(self, _: Sequence[str]) -> list[list[float]]:
        raise EmbeddingError("provider diagnostics must not reach the browser")


@pytest.fixture
def database_session() -> Iterator[Session]:
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def target_services(database_session: Session) -> tuple[InvestigationTarget, Service, Service]:
    target = InvestigationTarget(
        name="Knowledge API target",
        slug=f"knowledge-api-{uuid4()}",
        environment="staging",
        enabled=True,
    )
    service = Service(name="order-service", display_name="订单服务", enabled=True)
    target.services.append(service)
    other_target = InvestigationTarget(
        name="Other knowledge target",
        slug=f"other-knowledge-api-{uuid4()}",
        environment="production",
        enabled=True,
    )
    other_service = Service(name="payment-service", display_name="支付服务", enabled=True)
    other_target.services.append(other_service)
    database_session.add_all([target, other_target])
    database_session.commit()
    return target, service, other_service


@pytest.fixture
def api_client(
    database_session: Session, target_services: tuple[InvestigationTarget, Service, Service]
) -> Iterator[TestClient]:
    target, service, other_service = target_services

    def override_get_session() -> Iterator[Session]:
        yield database_session

    registry = TargetConfigRegistry(
        [
            InvestigationTargetConfig(
                target_id=target.id,
                slug=target.slug,
                environment=target.environment,
                services=[TargetServiceConfig(name=service.name)],
                logs=CapabilityConfig(
                    enabled=True,
                    adapter_type=AdapterType.FAULT_LAB,
                    provider_config_ref="knowledge-api-target",
                ),
                metrics=CapabilityConfig(
                    enabled=True,
                    adapter_type=AdapterType.FAULT_LAB,
                    provider_config_ref="knowledge-api-target",
                ),
            )
        ]
    )
    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_target_config_registry] = lambda: registry
    app.dependency_overrides[get_embedding_client] = FakeEmbeddingClient
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def _upload_data(
    target: InvestigationTarget,
    *,
    scope: str = "shared",
    service_id: Service | None = None,
    environment: str | None = None,
) -> dict[str, str]:
    data = {
        "target_id": str(target.id),
        "scope": scope,
        "environment": environment or target.environment,
        "document_type": "runbook",
        "version": "2026.09",
    }
    if service_id is not None:
        data["service_id"] = str(service_id.id)
    return data


def _markdown_file(filename: str = "order-triage.md") -> tuple[str, bytes, str]:
    return (
        filename,
        "# 订单服务排查手册\n\n## 现象\n\n检查请求错误率和下游超时。\n".encode(),
        "text/markdown",
    )


def test_upload_list_disable_and_scoped_retrieval_are_safe(
    api_client: TestClient,
    database_session: Session,
    target_services: tuple[InvestigationTarget, Service, Service],
) -> None:
    target, service, _ = target_services
    response = api_client.post(
        "/knowledge",
        data=_upload_data(target, scope="service", service_id=service),
        files={"file": _markdown_file()},
    )

    assert response.status_code == 201
    document = response.json()
    assert set(document) == {
        "id",
        "title",
        "target_id",
        "target_display_name",
        "scope",
        "service_id",
        "service_display_name",
        "environment",
        "document_type",
        "version",
        "status",
        "updated_at",
    }
    assert document["scope"] == "service"
    assert document["service_id"] == str(service.id)
    assert document["status"] == "enabled"
    for forbidden in ("embedding", "secret", "provider", "endpoint", "backend_config_key"):
        assert forbidden not in response.text.lower()

    listed = api_client.get("/knowledge", params={"target_id": str(target.id)})
    assert listed.status_code == 200
    assert [entry["id"] for entry in listed.json()] == [document["id"]]

    disabled = api_client.patch(f"/knowledge/{document['id']}/status", json={"status": "disabled"})
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    results = RAGService(database_session, FakeEmbeddingClient()).search_scoped(
        "错误率",
        scope=KnowledgeScope(
            target_id=target.id,
            service_id=service.id,
            environment=target.environment,
        ),
    )
    assert results == []


def test_upload_rejects_invalid_structured_scope_and_non_markdown(
    api_client: TestClient,
    database_session: Session,
    target_services: tuple[InvestigationTarget, Service, Service],
) -> None:
    target, service, other_service = target_services
    requests = (
        (
            _upload_data(target, scope="shared", service_id=service),
            _markdown_file(),
            "系统共享范围不能绑定服务。",
        ),
        (
            _upload_data(target, scope="service"),
            _markdown_file(),
            "指定服务范围必须选择服务。",
        ),
        (
            _upload_data(target, scope="service", service_id=other_service),
            _markdown_file(),
            "所选服务不属于当前调查目标或未启用。",
        ),
        (
            _upload_data(target),
            _markdown_file("not-markdown.pdf"),
            "仅支持上传 .md Markdown 文件。",
        ),
    )
    for data, file, detail in requests:
        response = api_client.post("/knowledge", data=data, files={"file": file})
        assert response.status_code == 422
        assert response.json() == {"detail": detail}

    wrong_environment = api_client.post(
        "/knowledge",
        data=_upload_data(target, environment="production"),
        files={"file": _markdown_file()},
    )
    assert wrong_environment.status_code == 422
    assert wrong_environment.json() == {"detail": "环境只能选择通用或当前调查目标环境。"}

    target.enabled = False
    database_session.commit()
    disabled_target = api_client.post(
        "/knowledge",
        data=_upload_data(target),
        files={"file": _markdown_file()},
    )
    assert disabled_target.status_code == 422
    assert disabled_target.json() == {"detail": "调查目标不存在或未启用。"}

    assert list(
        database_session.scalars(
            select(KnowledgeDocument).where(KnowledgeDocument.target_id == target.id)
        )
    ) == []
    assert list(
        database_session.scalars(
            select(KnowledgeChunk)
            .join(KnowledgeDocument)
            .where(KnowledgeDocument.target_id == target.id)
        )
    ) == []


def test_parse_and_embedding_failures_leave_no_document_or_chunk(
    api_client: TestClient,
    database_session: Session,
    target_services: tuple[InvestigationTarget, Service, Service],
) -> None:
    target, _, _ = target_services
    invalid_markdown = api_client.post(
        "/knowledge",
        data=_upload_data(target),
        files={"file": ("invalid.md", b"No title here", "text/markdown")},
    )
    assert invalid_markdown.status_code == 422
    assert "Markdown 文件无法解析" in invalid_markdown.json()["detail"]
    assert list(
        database_session.scalars(
            select(KnowledgeDocument).where(KnowledgeDocument.target_id == target.id)
        )
    ) == []
    assert list(
        database_session.scalars(
            select(KnowledgeChunk)
            .join(KnowledgeDocument)
            .where(KnowledgeDocument.target_id == target.id)
        )
    ) == []

    app.dependency_overrides[get_embedding_client] = FailingEmbeddingClient
    failed_embedding = api_client.post(
        "/knowledge",
        data=_upload_data(target),
        files={"file": _markdown_file()},
    )
    assert failed_embedding.status_code == 503
    assert failed_embedding.json() == {
        "detail": "知识文档暂时无法完成索引，请检查 Embedding 服务配置和网络连通性后重试。"
    }
    assert list(
        database_session.scalars(
            select(KnowledgeDocument).where(KnowledgeDocument.target_id == target.id)
        )
    ) == []
    assert list(
        database_session.scalars(
            select(KnowledgeChunk)
            .join(KnowledgeDocument)
            .where(KnowledgeDocument.target_id == target.id)
        )
    ) == []
