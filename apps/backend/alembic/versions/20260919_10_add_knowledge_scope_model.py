"""Add mandatory V2 scope ownership to knowledge documents.

Revision ID: 20260919_10
Revises: 20260919_09
Create Date: 2026-09-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260919_10"
down_revision: str | None = "20260919_09"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


COMPATIBILITY_TARGET_ID = "8e3208b4-4a01-5bf6-b11a-25ecce0fdceb"
COMPATIBILITY_TARGET_SLUG = "migration-compatibility-target"
VALID_DOCUMENT_TYPES = "'architecture', 'runbook', 'postmortem', 'config_note'"


def upgrade() -> None:
    """Backfill legacy documents into their dedicated compatibility target only."""
    op.add_column(
        "knowledge_documents",
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("knowledge_documents", sa.Column("scope", sa.String(length=20), nullable=True))
    op.add_column(
        "knowledge_documents",
        sa.Column("service_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("knowledge_documents", sa.Column("version", sa.String(length=100), nullable=True))
    op.add_column("knowledge_documents", sa.Column("status", sa.String(length=20), nullable=True))

    op.execute(
        f"""
        INSERT INTO investigation_targets (id, name, slug, description, environment, enabled)
        VALUES (
            '{COMPATIBILITY_TARGET_ID}'::uuid,
            'Migration compatibility target (legacy V1 knowledge)',
            '{COMPATIBILITY_TARGET_SLUG}',
            'Explicit compatibility scope for knowledge documents that predate V2 target ownership.',
            'legacy-compatibility',
            true
        )
        ON CONFLICT (slug) DO NOTHING
        """
    )
    op.execute(
        f"""
        INSERT INTO services (id, target_id, name, display_name, description, enabled)
        SELECT
            (
                substr(md5('v2-knowledge-compatibility-service:' || service), 1, 8) || '-' ||
                substr(md5('v2-knowledge-compatibility-service:' || service), 9, 4) || '-' ||
                substr(md5('v2-knowledge-compatibility-service:' || service), 13, 4) || '-' ||
                substr(md5('v2-knowledge-compatibility-service:' || service), 17, 4) || '-' ||
                substr(md5('v2-knowledge-compatibility-service:' || service), 21, 12)
            )::uuid,
            '{COMPATIBILITY_TARGET_ID}'::uuid,
            service,
            service,
            'Compatibility mapping for legacy KnowledgeDocument.service.',
            true
        FROM (SELECT DISTINCT service FROM knowledge_documents WHERE service IS NOT NULL) legacy_services
        ON CONFLICT (target_id, name) DO NOTHING
        """
    )
    op.execute(
        f"""
        UPDATE knowledge_documents
        SET target_id = '{COMPATIBILITY_TARGET_ID}'::uuid,
            scope = CASE WHEN service IS NULL THEN 'shared' ELSE 'service' END,
            service_id = services.id,
            environment = COALESCE(environment, 'common'),
            version = COALESCE(NULLIF(metadata_data ->> 'version', ''), 'legacy-v1'),
            status = 'enabled'
        FROM services
        WHERE services.target_id = '{COMPATIBILITY_TARGET_ID}'::uuid
          AND services.name = knowledge_documents.service
        """
    )
    op.execute(
        f"""
        UPDATE knowledge_documents
        SET target_id = '{COMPATIBILITY_TARGET_ID}'::uuid,
            scope = 'shared',
            environment = COALESCE(environment, 'common'),
            version = COALESCE(NULLIF(metadata_data ->> 'version', ''), 'legacy-v1'),
            status = 'enabled'
        WHERE service IS NULL
        """
    )
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM knowledge_documents
                WHERE target_id IS NULL OR scope IS NULL OR version IS NULL OR status IS NULL
            ) THEN
                RAISE EXCEPTION 'KnowledgeDocument compatibility backfill left an unscoped row';
            END IF;
            IF EXISTS (
                SELECT 1 FROM knowledge_documents
                WHERE document_type NOT IN ({VALID_DOCUMENT_TYPES})
            ) THEN
                RAISE EXCEPTION 'KnowledgeDocument contains a document_type unsupported by V2 scope model';
            END IF;
        END $$;
        """
    )

    op.alter_column("knowledge_documents", "target_id", nullable=False)
    op.alter_column("knowledge_documents", "scope", nullable=False)
    op.alter_column("knowledge_documents", "environment", nullable=False)
    op.alter_column("knowledge_documents", "version", nullable=False)
    op.alter_column("knowledge_documents", "status", nullable=False)
    op.create_index("ix_knowledge_documents_target_id", "knowledge_documents", ["target_id"])
    op.create_index("ix_knowledge_documents_service_id", "knowledge_documents", ["service_id"])
    op.create_foreign_key(
        "knowledge_documents_target_id_fkey",
        "knowledge_documents",
        "investigation_targets",
        ["target_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "knowledge_documents_service_id_target_id_fkey",
        "knowledge_documents",
        "services",
        ["service_id", "target_id"],
        ["id", "target_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_knowledge_documents_scope",
        "knowledge_documents",
        "scope IN ('shared', 'service')",
    )
    op.create_check_constraint(
        "ck_knowledge_documents_document_type",
        "knowledge_documents",
        f"document_type IN ({VALID_DOCUMENT_TYPES})",
    )
    op.create_check_constraint(
        "ck_knowledge_documents_status",
        "knowledge_documents",
        "status IN ('enabled', 'disabled')",
    )
    op.create_check_constraint(
        "ck_knowledge_documents_environment_not_blank",
        "knowledge_documents",
        "environment <> ''",
    )
    op.create_check_constraint(
        "ck_knowledge_documents_scope_service",
        "knowledge_documents",
        "(scope = 'shared' AND service_id IS NULL AND service IS NULL) "
        "OR (scope = 'service' AND service_id IS NOT NULL)",
    )


def downgrade() -> None:
    """Remove V2 scope columns while preserving the original legacy metadata columns."""
    for constraint_name in (
        "ck_knowledge_documents_scope_service",
        "ck_knowledge_documents_environment_not_blank",
        "ck_knowledge_documents_status",
        "ck_knowledge_documents_document_type",
        "ck_knowledge_documents_scope",
    ):
        op.drop_constraint(constraint_name, "knowledge_documents", type_="check")
    op.drop_constraint(
        "knowledge_documents_service_id_target_id_fkey",
        "knowledge_documents",
        type_="foreignkey",
    )
    op.drop_constraint("knowledge_documents_target_id_fkey", "knowledge_documents", type_="foreignkey")
    op.drop_index("ix_knowledge_documents_service_id", table_name="knowledge_documents")
    op.drop_index("ix_knowledge_documents_target_id", table_name="knowledge_documents")
    op.drop_column("knowledge_documents", "status")
    op.drop_column("knowledge_documents", "version")
    op.drop_column("knowledge_documents", "service_id")
    op.drop_column("knowledge_documents", "scope")
    op.drop_column("knowledge_documents", "target_id")
