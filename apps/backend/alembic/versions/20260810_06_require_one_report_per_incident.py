"""Require a single final Report per Incident.

Revision ID: 20260810_06
Revises: 20260809_05
"""
from collections.abc import Sequence
from alembic import op

revision: str = "20260810_06"
down_revision: str | None = "20260809_05"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

def upgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT incident_id FROM reports GROUP BY incident_id HAVING count(*) > 1) THEN RAISE EXCEPTION 'Cannot require one Report per Incident: duplicate Reports exist'; END IF; END $$;""")
    op.create_unique_constraint("uq_reports_incident_id", "reports", ["incident_id"])

def downgrade() -> None:
    op.drop_constraint("uq_reports_incident_id", "reports", type_="unique")
