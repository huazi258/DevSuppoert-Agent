"""Create the initial DevSupport domain and knowledge schema.

Revision ID: 20260808_01
Revises:
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260808_01"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def timestamp_columns() -> list[sa.Column[sa.DateTime]]:
    """Return the common timezone-aware audit columns."""
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def incident_record_columns() -> list[sa.Column[object]]:
    """Return common columns for records related to an incident."""
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "incident_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        *timestamp_columns(),
    ]


def upgrade() -> None:
    """Create domain tables, knowledge tables, and the pgvector extension."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "incidents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("service", sa.String(length=100), nullable=False),
        sa.Column("environment", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        *timestamp_columns(),
    )
    op.create_table(
        "hypotheses",
        *incident_record_columns(),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
    )
    op.create_table(
        "evidence",
        *incident_record_columns(),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
    )
    op.create_table(
        "tool_calls",
        *incident_record_columns(),
        sa.Column("tool_name", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("input_data", postgresql.JSONB(), nullable=False),
        sa.Column("output_data", postgresql.JSONB(), nullable=False),
    )
    op.create_table(
        "approvals",
        *incident_record_columns(),
        sa.Column("decision", sa.String(length=50), nullable=False),
    )
    op.create_table(
        "actions",
        *incident_record_columns(),
        sa.Column("action_type", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
    )
    op.create_table(
        "verifications",
        *incident_record_columns(),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
    )
    op.create_table(
        "reports",
        *incident_record_columns(),
        sa.Column("content", sa.Text(), nullable=False),
    )
    op.create_table(
        "knowledge_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("source_path", sa.String(length=500), nullable=False, unique=True),
        sa.Column("document_type", sa.String(length=100), nullable=False),
        sa.Column("service", sa.String(length=100), nullable=True),
        sa.Column("environment", sa.String(length=50), nullable=True),
        sa.Column("metadata_data", postgresql.JSONB(), nullable=False),
        *timestamp_columns(),
    )
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata_data", postgresql.JSONB(), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column(
            "text_search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english'::regconfig, content)", persisted=True),
            nullable=False,
        ),
        *timestamp_columns(),
    )
    for table_name in (
        "hypotheses",
        "evidence",
        "tool_calls",
        "approvals",
        "actions",
        "verifications",
        "reports",
    ):
        op.create_index(f"ix_{table_name}_incident_id", table_name, ["incident_id"])
    op.create_index("ix_knowledge_chunks_document_id", "knowledge_chunks", ["document_id"])
    op.create_index(
        "ix_knowledge_chunks_text_search_vector",
        "knowledge_chunks",
        ["text_search_vector"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    """Drop all initial schema objects and the pgvector extension."""
    op.drop_index("ix_knowledge_chunks_text_search_vector", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_document_id", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")
    op.drop_table("knowledge_documents")
    for table_name in (
        "reports",
        "verifications",
        "actions",
        "approvals",
        "tool_calls",
        "evidence",
        "hypotheses",
    ):
        op.drop_index(f"ix_{table_name}_incident_id", table_name=table_name)
        op.drop_table(table_name)
    op.drop_table("incidents")
    op.execute("DROP EXTENSION IF EXISTS vector")
