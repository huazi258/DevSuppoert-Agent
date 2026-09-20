"""V2 Markdown knowledge document management endpoints."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from devsupport_backend.config import settings
from devsupport_backend.database import get_session
from devsupport_backend.models import InvestigationTarget, KnowledgeDocument, Service
from devsupport_backend.rag.embeddings import (
    EmbeddingClient,
    EmbeddingError,
    OpenAICompatibleEmbeddingClient,
)
from devsupport_backend.rag.ingest import ingest_uploaded_markdown
from devsupport_backend.rag.markdown import KnowledgeDocumentParseError
from devsupport_backend.schemas.knowledge import (
    KnowledgeDocumentResponse,
    KnowledgeDocumentStatusUpdate,
    KnowledgeDocumentType,
    KnowledgeScope,
    KnowledgeUploadMetadata,
)
from devsupport_backend.target_config import TargetConfigError, TargetConfigRegistry

router = APIRouter(prefix="/knowledge", tags=["knowledge"])
SessionDependency = Annotated[Session, Depends(get_session)]


class KnowledgeValidationError(ValueError):
    """A structured upload field does not fit the configured investigation boundary."""


def get_target_config_registry() -> TargetConfigRegistry:
    """Load deployment-owned target configuration without returning it to callers."""
    return TargetConfigRegistry.from_settings(settings)


def get_embedding_client() -> EmbeddingClient:
    """Create the configured embedding boundary only while processing an upload."""
    return OpenAICompatibleEmbeddingClient.from_settings(settings)


TargetConfigRegistryDependency = Annotated[
    TargetConfigRegistry, Depends(get_target_config_registry)
]
EmbeddingClientDependency = Annotated[EmbeddingClient, Depends(get_embedding_client)]


@router.get("", response_model=list[KnowledgeDocumentResponse])
def list_knowledge_documents(
    session: SessionDependency,
    target_id: UUID | None = None,
) -> list[KnowledgeDocumentResponse]:
    """List safe document metadata, optionally limited to one configured target."""
    statement = (
        select(KnowledgeDocument, InvestigationTarget, Service)
        .join(InvestigationTarget, KnowledgeDocument.target_id == InvestigationTarget.id)
        .outerjoin(Service, KnowledgeDocument.service_id == Service.id)
        .order_by(KnowledgeDocument.updated_at.desc(), KnowledgeDocument.id)
    )
    if target_id is not None:
        statement = statement.where(KnowledgeDocument.target_id == target_id)
    return [
        _document_response(document, target, service)
        for document, target, service in session.execute(statement)
    ]


@router.post("", response_model=KnowledgeDocumentResponse, status_code=status.HTTP_201_CREATED)
async def upload_knowledge_document(
    session: SessionDependency,
    target_configs: TargetConfigRegistryDependency,
    embedding_client: EmbeddingClientDependency,
    target_id: Annotated[UUID, Form()],
    scope: Annotated[KnowledgeScope, Form()],
    environment: Annotated[str, Form()],
    document_type: Annotated[KnowledgeDocumentType, Form()],
    version: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    service_id: Annotated[UUID | None, Form()] = None,
) -> KnowledgeDocumentResponse:
    """Validate, parse, embed, and atomically index one Markdown document."""
    metadata = KnowledgeUploadMetadata(
        target_id=target_id,
        scope=scope,
        service_id=service_id,
        environment=environment.strip(),
        document_type=document_type,
        version=version.strip(),
    )
    try:
        target, service = _validate_upload_scope(session, target_configs, metadata)
        filename = _markdown_filename(file.filename)
        raw_content = (await file.read()).decode("utf-8")
        document = ingest_uploaded_markdown(
            session,
            raw_content=raw_content,
            source_path=f"uploads/{target.id}/{uuid4()}-{filename}",
            target_id=target.id,
            scope=metadata.scope,
            service_id=service.id if service is not None else None,
            service_name=service.name if service is not None else None,
            environment=metadata.environment,
            document_type=metadata.document_type,
            version=metadata.version,
            embedding_client=embedding_client,
        )
    except KnowledgeValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error
    except (UnicodeDecodeError, KnowledgeDocumentParseError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Markdown 文件无法解析，请检查 UTF-8 编码、一级标题和正文。",
        ) from None
    except EmbeddingError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="知识文档暂时无法完成索引，请稍后重试。",
        ) from None
    except Exception:
        session.rollback()
        logging.getLogger(__name__).exception("Knowledge document ingestion failed safely")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="知识文档摄取失败，未保存任何可检索内容。",
        ) from None
    return _document_response(document, target, service)


@router.patch("/{document_id}/status", response_model=KnowledgeDocumentResponse)
def update_knowledge_document_status(
    document_id: UUID,
    payload: KnowledgeDocumentStatusUpdate,
    session: SessionDependency,
) -> KnowledgeDocumentResponse:
    """Enable or disable a document; scoped retrieval reads this status on every request."""
    document = session.get(KnowledgeDocument, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="知识文档不存在。")
    document.status = payload.status
    session.commit()
    session.refresh(document)
    target = session.get(InvestigationTarget, document.target_id)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="知识文档所属调查目标不存在。",
        )
    service = session.get(Service, document.service_id) if document.service_id is not None else None
    return _document_response(document, target, service)


def _validate_upload_scope(
    session: Session,
    target_configs: TargetConfigRegistry,
    metadata: KnowledgeUploadMetadata,
) -> tuple[InvestigationTarget, Service | None]:
    target = session.get(InvestigationTarget, metadata.target_id)
    if target is None or not target.enabled:
        raise KnowledgeValidationError("调查目标不存在或未启用。")
    try:
        target_config = target_configs.get(target_id=target.id, slug=target.slug)
        if target_config.environment != target.environment:
            raise TargetConfigError("target environment does not match deployment configuration")
    except TargetConfigError as error:
        raise KnowledgeValidationError("调查目标未完成部署配置，无法上传知识文档。") from error
    if metadata.environment not in {"common", target.environment}:
        raise KnowledgeValidationError("环境只能选择通用或当前调查目标环境。")
    if metadata.scope == "shared":
        if metadata.service_id is not None:
            raise KnowledgeValidationError("系统共享范围不能绑定服务。")
        return target, None
    if metadata.service_id is None:
        raise KnowledgeValidationError("指定服务范围必须选择服务。")
    service = session.scalar(
        select(Service).where(Service.id == metadata.service_id, Service.target_id == target.id)
    )
    if service is None or not service.enabled:
        raise KnowledgeValidationError("所选服务不属于当前调查目标或未启用。")
    try:
        target_configs.require_service(service.name, target_id=target.id)
    except TargetConfigError as error:
        raise KnowledgeValidationError("所选服务不在当前调查目标的允许范围内。") from error
    return target, service


def _markdown_filename(filename: str | None) -> str:
    safe_filename = Path(filename or "").name
    if not safe_filename or Path(safe_filename).suffix.lower() != ".md":
        raise KnowledgeValidationError("仅支持上传 .md Markdown 文件。")
    return safe_filename


def _document_response(
    document: KnowledgeDocument,
    target: InvestigationTarget,
    service: Service | None,
) -> KnowledgeDocumentResponse:
    return KnowledgeDocumentResponse(
        id=document.id,
        title=document.title,
        target_id=document.target_id,
        target_display_name=target.name,
        scope=document.scope,
        service_id=document.service_id,
        service_display_name=service.display_name if service is not None else None,
        environment=document.environment,
        document_type=document.document_type,
        version=document.version,
        status=document.status,
        updated_at=document.updated_at,
    )
