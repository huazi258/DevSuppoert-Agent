"""Require one audit-safe final Verification for each Action.

Revision ID: 20260809_05
Revises: 20260809_04
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260809_05"
down_revision: str | None = "20260809_04"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM verifications WHERE action_id IS NULL) THEN
                RAISE EXCEPTION 'Cannot require verifications.action_id: orphan Verification records exist';
            END IF;
            IF EXISTS (SELECT action_id FROM verifications GROUP BY action_id HAVING count(*) > 1) THEN
                RAISE EXCEPTION 'Cannot require one Verification per Action: duplicate Verification records exist';
            END IF;
        END $$;
    """)
    op.drop_constraint("verifications_action_id_fkey", "verifications", type_="foreignkey")
    op.alter_column("verifications", "action_id", existing_type=sa.UUID(), nullable=False)
    op.create_foreign_key("verifications_action_id_fkey", "verifications", "actions", ["action_id"], ["id"], ondelete="RESTRICT")
    op.create_unique_constraint("uq_verifications_action_id", "verifications", ["action_id"])


def downgrade() -> None:
    op.drop_constraint("uq_verifications_action_id", "verifications", type_="unique")
    op.drop_constraint("verifications_action_id_fkey", "verifications", type_="foreignkey")
    op.alter_column("verifications", "action_id", existing_type=sa.UUID(), nullable=True)
    op.create_foreign_key("verifications_action_id_fkey", "verifications", "actions", ["action_id"], ["id"], ondelete="SET NULL")
