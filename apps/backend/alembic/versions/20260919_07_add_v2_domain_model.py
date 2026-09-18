"""Add the V2 investigation target, round, observation, and report model.

Revision ID: 20260919_07
Revises: 20260810_06
Create Date: 2026-09-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260919_07"
down_revision: str | None = "20260810_06"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


COMPATIBILITY_TARGET_ID = "8e3208b4-4a01-5bf6-b11a-25ecce0fdceb"
COMPATIBILITY_TARGET_SLUG = "migration-compatibility-target"


def upgrade() -> None:
    """Preserve V1 rows while attaching them to explicit V2 domain identities."""
    op.create_table(
        "investigation_targets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("environment", sa.String(length=50), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
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
        sa.UniqueConstraint("slug", name="uq_investigation_targets_slug"),
    )
    op.create_table(
        "services",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
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
        sa.ForeignKeyConstraint(["target_id"], ["investigation_targets.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("target_id", "name", name="uq_services_target_id_name"),
    )
    op.create_index("ix_services_target_id", "services", ["target_id"])

    op.add_column("incidents", sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column(
        "incidents", sa.Column("service_id", postgresql.UUID(as_uuid=True), nullable=True)
    )

    op.create_table(
        "investigation_rounds",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("thread_id", sa.String(length=255), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminal_reason", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "incident_id", "round_number", name="uq_investigation_rounds_incident_number"
        ),
        sa.UniqueConstraint("thread_id", name="uq_investigation_rounds_thread_id"),
    )
    op.create_index("ix_investigation_rounds_incident_id", "investigation_rounds", ["incident_id"])

    op.create_table(
        "observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("round_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "context_data",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["round_id"], ["investigation_rounds.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_observations_incident_id", "observations", ["incident_id"])
    op.create_index("ix_observations_round_id", "observations", ["round_id"])

    for table_name in ("hypotheses", "evidence", "tool_calls"):
        op.add_column(
            table_name, sa.Column("round_id", postgresql.UUID(as_uuid=True), nullable=True)
        )
        op.create_foreign_key(
            f"{table_name}_round_id_fkey",
            table_name,
            "investigation_rounds",
            ["round_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index(f"ix_{table_name}_round_id", table_name, ["round_id"])

    op.add_column("reports", sa.Column("round_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("reports", sa.Column("version", sa.Integer(), nullable=True))

    # A fixed target ID plus deterministic service and round IDs make replays safe and auditable.
    op.execute(
        f"""
        INSERT INTO investigation_targets (id, name, slug, description, environment, enabled)
        VALUES (
            '{COMPATIBILITY_TARGET_ID}'::uuid,
            'Migration compatibility target (legacy V1 incidents)',
            '{COMPATIBILITY_TARGET_SLUG}',
            'Automatically created by the V2 migration; not a configured production target.',
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
                substr(md5('v2-compatibility-service:' || service), 1, 8) || '-' ||
                substr(md5('v2-compatibility-service:' || service), 9, 4) || '-' ||
                substr(md5('v2-compatibility-service:' || service), 13, 4) || '-' ||
                substr(md5('v2-compatibility-service:' || service), 17, 4) || '-' ||
                substr(md5('v2-compatibility-service:' || service), 21, 12)
            )::uuid,
            '{COMPATIBILITY_TARGET_ID}'::uuid,
            service,
            service,
            'Compatibility mapping for the legacy Incident.service field.',
            true
        FROM (SELECT DISTINCT service FROM incidents) legacy_services
        ON CONFLICT (target_id, name) DO NOTHING
        """
    )
    op.execute(
        f"""
        UPDATE incidents
        SET target_id = '{COMPATIBILITY_TARGET_ID}'::uuid,
            service_id = services.id
        FROM services
        WHERE services.target_id = '{COMPATIBILITY_TARGET_ID}'::uuid
          AND services.name = incidents.service
        """
    )
    op.alter_column("incidents", "target_id", nullable=False)
    op.alter_column("incidents", "service_id", nullable=False)
    op.create_foreign_key(
        "incidents_target_id_fkey",
        "incidents",
        "investigation_targets",
        ["target_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "incidents_service_id_fkey",
        "incidents",
        "services",
        ["service_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_incidents_target_id", "incidents", ["target_id"])
    op.create_index("ix_incidents_service_id", "incidents", ["service_id"])

    op.execute(
        """
        INSERT INTO investigation_rounds (id, incident_id, round_number, status, thread_id, started_at)
        SELECT
            (
                substr(md5('v2-compatibility-round:' || id::text), 1, 8) || '-' ||
                substr(md5('v2-compatibility-round:' || id::text), 9, 4) || '-' ||
                substr(md5('v2-compatibility-round:' || id::text), 13, 4) || '-' ||
                substr(md5('v2-compatibility-round:' || id::text), 17, 4) || '-' ||
                substr(md5('v2-compatibility-round:' || id::text), 21, 12)
            )::uuid,
            id,
            1,
            status,
            thread_id,
            created_at
        FROM incidents
        """
    )
    for table_name in ("hypotheses", "evidence", "tool_calls"):
        op.execute(
            f"""
            UPDATE {table_name}
            SET round_id = investigation_rounds.id
            FROM investigation_rounds
            WHERE investigation_rounds.incident_id = {table_name}.incident_id
              AND investigation_rounds.round_number = 1
            """
        )
    op.execute(
        """
        UPDATE reports
        SET round_id = investigation_rounds.id,
            version = investigation_rounds.round_number
        FROM investigation_rounds
        WHERE investigation_rounds.incident_id = reports.incident_id
          AND investigation_rounds.round_number = 1
        """
    )
    op.alter_column("reports", "round_id", nullable=False)
    op.alter_column("reports", "version", nullable=False)
    op.drop_constraint("uq_reports_incident_id", "reports", type_="unique")
    op.create_foreign_key(
        "reports_round_id_fkey",
        "reports",
        "investigation_rounds",
        ["round_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_reports_round_id", "reports", ["round_id"])
    op.create_unique_constraint("uq_reports_round_id", "reports", ["round_id"])
    op.create_unique_constraint(
        "uq_reports_incident_version", "reports", ["incident_id", "version"]
    )


def downgrade() -> None:
    """Restore the V1 schema only when V2 report history has not become lossy."""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT incident_id FROM reports GROUP BY incident_id HAVING count(*) > 1) THEN
                RAISE EXCEPTION 'Cannot downgrade V2 reports: multiple report snapshots exist per Incident';
            END IF;
        END $$;
        """
    )
    op.drop_constraint("uq_reports_incident_version", "reports", type_="unique")
    op.drop_constraint("uq_reports_round_id", "reports", type_="unique")
    op.drop_index("ix_reports_round_id", table_name="reports")
    op.drop_constraint("reports_round_id_fkey", "reports", type_="foreignkey")
    op.drop_column("reports", "version")
    op.drop_column("reports", "round_id")
    op.create_unique_constraint("uq_reports_incident_id", "reports", ["incident_id"])

    for table_name in ("tool_calls", "evidence", "hypotheses"):
        op.drop_index(f"ix_{table_name}_round_id", table_name=table_name)
        op.drop_constraint(f"{table_name}_round_id_fkey", table_name, type_="foreignkey")
        op.drop_column(table_name, "round_id")

    op.drop_index("ix_incidents_service_id", table_name="incidents")
    op.drop_index("ix_incidents_target_id", table_name="incidents")
    op.drop_constraint("incidents_service_id_fkey", "incidents", type_="foreignkey")
    op.drop_constraint("incidents_target_id_fkey", "incidents", type_="foreignkey")
    op.drop_column("incidents", "service_id")
    op.drop_column("incidents", "target_id")

    op.drop_index("ix_observations_round_id", table_name="observations")
    op.drop_index("ix_observations_incident_id", table_name="observations")
    op.drop_table("observations")
    op.drop_index("ix_investigation_rounds_incident_id", table_name="investigation_rounds")
    op.drop_table("investigation_rounds")
    op.drop_index("ix_services_target_id", table_name="services")
    op.drop_table("services")
    op.drop_table("investigation_targets")
