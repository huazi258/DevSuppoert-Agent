"""Require stable thread identifiers for every incident.

Revision ID: 20260809_03
Revises: 20260808_02
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260809_03"
down_revision: str | None = "20260808_02"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Backfill legacy records before enforcing stable, unique thread IDs."""
    op.execute("UPDATE incidents SET thread_id = id::text WHERE thread_id IS NULL")
    op.alter_column("incidents", "thread_id", existing_type=sa.String(length=255), nullable=False)
    op.create_unique_constraint("uq_incidents_thread_id", "incidents", ["thread_id"])


def downgrade() -> None:
    """Restore the previous nullable, non-unique thread ID column."""
    op.drop_constraint("uq_incidents_thread_id", "incidents", type_="unique")
    op.alter_column("incidents", "thread_id", existing_type=sa.String(length=255), nullable=True)
