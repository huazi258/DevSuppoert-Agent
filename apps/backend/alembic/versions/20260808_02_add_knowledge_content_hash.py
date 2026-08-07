"""Add idempotency fields for knowledge ingestion.

Revision ID: 20260808_02
Revises: 20260808_01
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260808_02"
down_revision: str | None = "20260808_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Store source content hashes and enforce stable chunk positions."""
    op.add_column(
        "knowledge_documents",
        sa.Column("content_hash", sa.String(length=64), nullable=False, server_default=""),
    )
    op.alter_column("knowledge_documents", "content_hash", server_default=None)
    op.create_unique_constraint(
        "uq_knowledge_chunks_document_index",
        "knowledge_chunks",
        ["document_id", "chunk_index"],
    )


def downgrade() -> None:
    """Remove knowledge-ingestion idempotency fields."""
    op.drop_constraint(
        "uq_knowledge_chunks_document_index",
        "knowledge_chunks",
        type_="unique",
    )
    op.drop_column("knowledge_documents", "content_hash")
