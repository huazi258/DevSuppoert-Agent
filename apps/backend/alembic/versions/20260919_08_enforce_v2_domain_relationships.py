"""Enforce target, service, incident, and round ownership consistency.

Revision ID: 20260919_08
Revises: 20260919_07
Create Date: 2026-09-19
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260919_08"
down_revision: str | None = "20260919_07"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


ROUND_OWNED_TABLES = ("observations", "hypotheses", "evidence", "tool_calls", "reports")


def upgrade() -> None:
    """Use composite foreign keys so all direct writers preserve V2 ownership boundaries."""
    op.create_unique_constraint("uq_services_id_target_id", "services", ["id", "target_id"])
    op.drop_constraint("incidents_service_id_fkey", "incidents", type_="foreignkey")
    op.create_foreign_key(
        "incidents_service_id_target_id_fkey",
        "incidents",
        "services",
        ["service_id", "target_id"],
        ["id", "target_id"],
        ondelete="RESTRICT",
    )

    op.create_unique_constraint(
        "uq_investigation_rounds_id_incident_id",
        "investigation_rounds",
        ["id", "incident_id"],
    )
    for table_name in ROUND_OWNED_TABLES:
        op.drop_constraint(f"{table_name}_round_id_fkey", table_name, type_="foreignkey")
        op.create_foreign_key(
            f"{table_name}_round_id_incident_id_fkey",
            table_name,
            "investigation_rounds",
            ["round_id", "incident_id"],
            ["id", "incident_id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    """Restore the former independent foreign keys."""
    for table_name in ROUND_OWNED_TABLES:
        op.drop_constraint(
            f"{table_name}_round_id_incident_id_fkey", table_name, type_="foreignkey"
        )
        ondelete = "RESTRICT" if table_name == "reports" else "SET NULL"
        op.create_foreign_key(
            f"{table_name}_round_id_fkey",
            table_name,
            "investigation_rounds",
            ["round_id"],
            ["id"],
            ondelete=ondelete,
        )
    op.drop_constraint(
        "uq_investigation_rounds_id_incident_id",
        "investigation_rounds",
        type_="unique",
    )

    op.drop_constraint("incidents_service_id_target_id_fkey", "incidents", type_="foreignkey")
    op.create_foreign_key(
        "incidents_service_id_fkey",
        "incidents",
        "services",
        ["service_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint("uq_services_id_target_id", "services", type_="unique")
