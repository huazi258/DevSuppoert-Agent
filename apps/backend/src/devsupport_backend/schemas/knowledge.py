"""Structured, safe request and response schemas for V2 knowledge management."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

KnowledgeScope = Literal["shared", "service"]
KnowledgeDocumentType = Literal["architecture", "runbook", "postmortem", "config_note"]
KnowledgeDocumentStatus = Literal["enabled", "disabled"]


class KnowledgeDocumentResponse(BaseModel):
    """Safe metadata projection; content, embeddings, and deployment details stay private."""

    id: UUID
    title: str
    target_id: UUID
    target_display_name: str
    scope: KnowledgeScope
    service_id: UUID | None = None
    service_display_name: str | None = None
    environment: str
    document_type: KnowledgeDocumentType
    version: str
    status: KnowledgeDocumentStatus
    updated_at: datetime


class KnowledgeDocumentStatusUpdate(BaseModel):
    """The only mutable document property exposed to team users in M5.4."""

    status: KnowledgeDocumentStatus


class KnowledgeUploadMetadata(BaseModel):
    """Validated structured fields accepted alongside an uploaded Markdown file."""

    target_id: UUID
    scope: KnowledgeScope
    service_id: UUID | None = None
    environment: str = Field(min_length=1, max_length=50)
    document_type: KnowledgeDocumentType
    version: str = Field(min_length=1, max_length=100)

    @field_validator("environment", "version")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized
