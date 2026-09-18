"""Add constrained V2 investigation lifecycle projections.

Revision ID: 20260919_09
Revises: 20260919_08
Create Date: 2026-09-19
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260919_09"
down_revision: str | None = "20260919_08"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


V2_STATUS_VALUES = "'OPEN', 'INVESTIGATING', 'CONCLUDED', 'INCONCLUSIVE', 'FAILED'"


def _legacy_status_projection(column_name: str) -> str:
    """Return SQL which projects retained V1 remediation values into safe V2 lifecycle states."""
    return f"""
        CASE {column_name}
            WHEN 'OPEN' THEN 'OPEN'
            WHEN 'INVESTIGATING' THEN 'INVESTIGATING'
            WHEN 'WAITING_APPROVAL' THEN 'INVESTIGATING'
            WHEN 'REMEDIATING' THEN 'INVESTIGATING'
            WHEN 'VERIFYING' THEN 'INVESTIGATING'
            WHEN 'RESOLVED' THEN 'CONCLUDED'
            WHEN 'NEEDS_MANUAL_ACTION' THEN 'INCONCLUSIVE'
            ELSE 'FAILED'
        END
    """


def upgrade() -> None:
    """Constrain V2 projections while retaining the original V1 remediation status column."""
    op.add_column(
        "incidents",
        sa.Column(
            "investigation_status",
            sa.String(length=50),
            nullable=False,
            server_default="OPEN",
        ),
    )
    op.execute("UPDATE incidents SET investigation_status = " + _legacy_status_projection("status"))
    op.alter_column("incidents", "investigation_status", server_default=None)
    op.create_check_constraint(
        "ck_incidents_investigation_status_v2",
        "incidents",
        f"investigation_status IN ({V2_STATUS_VALUES})",
    )

    op.execute("UPDATE investigation_rounds SET status = " + _legacy_status_projection("status"))
    op.create_check_constraint(
        "ck_investigation_rounds_status_v2",
        "investigation_rounds",
        f"status IN ({V2_STATUS_VALUES})",
    )


def downgrade() -> None:
    """Remove the V2 projection and restore the unconstrained legacy Round field."""
    op.drop_constraint("ck_investigation_rounds_status_v2", "investigation_rounds", type_="check")
    op.drop_constraint("ck_incidents_investigation_status_v2", "incidents", type_="check")
    op.drop_column("incidents", "investigation_status")
