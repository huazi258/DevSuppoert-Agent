"""Initial PostgreSQL domain and knowledge models for DevSupport Agent V0."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIMENSIONS = 1536


class Base(DeclarativeBase):
    """Base class for all persisted backend data."""


class TimestampMixin:
    """Add UTC-capable audit timestamps to persisted records."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Incident(TimestampMixin, Base):
    __tablename__ = "incidents"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    service: Mapped[str] = mapped_column(String(100), nullable=False)
    environment: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="OPEN", nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    hypotheses: Mapped[list["Hypothesis"]] = relationship(back_populates="incident")
    evidence_items: Mapped[list["Evidence"]] = relationship(back_populates="incident")
    tool_calls: Mapped[list["ToolCall"]] = relationship(back_populates="incident")
    approvals: Mapped[list["Approval"]] = relationship(back_populates="incident")
    actions: Mapped[list["Action"]] = relationship(back_populates="incident")
    verifications: Mapped[list["Verification"]] = relationship(back_populates="incident")
    reports: Mapped[list["Report"]] = relationship(back_populates="incident")


class IncidentRecordMixin:
    """Common foreign-key identity for audit records owned by an incident."""

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    incident_id: Mapped[UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)


class Hypothesis(TimestampMixin, IncidentRecordMixin, Base):
    __tablename__ = "hypotheses"

    summary: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="OPEN", nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    incident: Mapped[Incident] = relationship(back_populates="hypotheses")


class Evidence(TimestampMixin, IncidentRecordMixin, Base):
    __tablename__ = "evidence"

    source: Mapped[str] = mapped_column(String(100), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    incident: Mapped[Incident] = relationship(back_populates="evidence_items")


class ToolCall(TimestampMixin, IncidentRecordMixin, Base):
    __tablename__ = "tool_calls"

    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    input_data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    output_data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    incident: Mapped[Incident] = relationship(back_populates="tool_calls")


class Approval(TimestampMixin, IncidentRecordMixin, Base):
    __tablename__ = "approvals"

    decision: Mapped[str] = mapped_column(String(50), nullable=False)

    incident: Mapped[Incident] = relationship(back_populates="approvals")


class Action(TimestampMixin, IncidentRecordMixin, Base):
    __tablename__ = "actions"

    action_type: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)

    incident: Mapped[Incident] = relationship(back_populates="actions")


class Verification(TimestampMixin, IncidentRecordMixin, Base):
    __tablename__ = "verifications"

    status: Mapped[str] = mapped_column(String(50), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)

    incident: Mapped[Incident] = relationship(back_populates="verifications")


class Report(TimestampMixin, IncidentRecordMixin, Base):
    __tablename__ = "reports"

    content: Mapped[str] = mapped_column(Text, nullable=False)

    incident: Mapped[Incident] = relationship(back_populates="reports")


class KnowledgeDocument(TimestampMixin, Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    source_path: Mapped[str] = mapped_column(String(500), unique=True, nullable=False)
    document_type: Mapped[str] = mapped_column(String(100), nullable=False)
    service: Mapped[str | None] = mapped_column(String(100), nullable=True)
    environment: Mapped[str | None] = mapped_column(String(50), nullable=True)
    metadata_data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    chunks: Mapped[list["KnowledgeChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class KnowledgeChunk(TimestampMixin, Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        Index(
            "ix_knowledge_chunks_text_search_vector",
            "text_search_vector",
            postgresql_using="gin",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIMENSIONS), nullable=True
    )
    text_search_vector: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english'::regconfig, content)", persisted=True),
        nullable=False,
    )

    document: Mapped[KnowledgeDocument] = relationship(back_populates="chunks")
