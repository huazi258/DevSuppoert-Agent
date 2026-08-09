"""Require every human approval to remain bound to exactly one Action.

Revision ID: 20260809_04
Revises: 20260809_03
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260809_04"
down_revision: str | None = "20260809_03"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Fail safely on legacy orphan/duplicate records before tightening the contract."""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM approvals WHERE action_id IS NULL) THEN
                RAISE EXCEPTION
                    'Cannot require approvals.action_id: legacy orphan Approval records exist';
            END IF;
            IF EXISTS (
                SELECT action_id
                FROM approvals
                GROUP BY action_id
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'Cannot require one Approval per Action: duplicate Approval records exist';
            END IF;
        END $$;
        """
    )
    op.drop_constraint("approvals_action_id_fkey", "approvals", type_="foreignkey")
    op.alter_column(
        "approvals",
        "action_id",
        existing_type=sa.UUID(),
        nullable=False,
    )
    op.create_foreign_key(
        "approvals_action_id_fkey",
        "approvals",
        "actions",
        ["action_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint("uq_approvals_action_id", "approvals", ["action_id"])


def downgrade() -> None:
    """Restore the former nullable approval Action binding."""
    op.drop_constraint("uq_approvals_action_id", "approvals", type_="unique")
    op.drop_constraint("approvals_action_id_fkey", "approvals", type_="foreignkey")
    op.alter_column(
        "approvals",
        "action_id",
        existing_type=sa.UUID(),
        nullable=True,
    )
    op.create_foreign_key(
        "approvals_action_id_fkey",
        "approvals",
        "actions",
        ["action_id"],
        ["id"],
        ondelete="SET NULL",
    )
