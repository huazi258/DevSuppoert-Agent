"""PostgreSQL domain and knowledge models for DevSupport Agent V2."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Computed,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    inspect,
    select,
)
from sqlalchemy import (
    Enum as SqlEnum,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from devsupport_backend.investigation_status import InvestigationStatus, legacy_status_projection


class Base(DeclarativeBase):
    """Base class for all persisted backend data."""


class TimestampMixin:
    """Add UTC-capable audit timestamps to persisted records."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


MIGRATION_COMPATIBILITY_TARGET_SLUG = "migration-compatibility-target"
INVESTIGATION_STATUS_TYPE = SqlEnum(
    InvestigationStatus,
    name="investigation_status",
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
)


class InvestigationTarget(TimestampMixin, Base):
    """A deployment-configured microservice environment available for investigation."""

    __tablename__ = "investigation_targets"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    environment: Mapped[str] = mapped_column(String(50), nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)

    services: Mapped[list["Service"]] = relationship(
        back_populates="target", cascade="all, delete-orphan"
    )
    incidents: Mapped[list["Incident"]] = relationship(back_populates="target")


class Service(TimestampMixin, Base):
    """A target-scoped service identifier, never a free-form data-source connection."""

    __tablename__ = "services"
    __table_args__ = (
        UniqueConstraint("target_id", "name", name="uq_services_target_id_name"),
        UniqueConstraint("id", "target_id", name="uq_services_id_target_id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    target_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_targets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)

    target: Mapped[InvestigationTarget] = relationship(back_populates="services")
    incidents: Mapped[list["Incident"]] = relationship(
        back_populates="service_record", overlaps="incidents,target"
    )


class Incident(TimestampMixin, Base):
    __tablename__ = "incidents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["service_id", "target_id"],
            ["services.id", "services.target_id"],
            name="incidents_service_id_target_id_fkey",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    service: Mapped[str] = mapped_column(String(100), nullable=False)
    environment: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="OPEN", nullable=False)
    # Retained V1 remediation state lives in status; this is the V2 product lifecycle projection.
    investigation_status: Mapped[InvestigationStatus] = mapped_column(
        INVESTIGATION_STATUS_TYPE,
        default=InvestigationStatus.OPEN,
        nullable=False,
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    time_range_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    time_range_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    target_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_targets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    service_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    # Retained during the V1 workflow transition.  V2 selection is represented by service_id.
    thread_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    target: Mapped[InvestigationTarget] = relationship(
        back_populates="incidents", overlaps="incidents,service_record"
    )
    service_record: Mapped[Service] = relationship(
        back_populates="incidents", overlaps="incidents,target"
    )
    rounds: Mapped[list["InvestigationRound"]] = relationship(
        back_populates="incident",
        cascade="all, delete-orphan",
        order_by="InvestigationRound.round_number",
    )
    observations: Mapped[list["Observation"]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )
    hypotheses: Mapped[list["Hypothesis"]] = relationship(back_populates="incident")
    evidence_items: Mapped[list["Evidence"]] = relationship(back_populates="incident")
    tool_calls: Mapped[list["ToolCall"]] = relationship(back_populates="incident")
    approvals: Mapped[list["Approval"]] = relationship(back_populates="incident")
    actions: Mapped[list["Action"]] = relationship(back_populates="incident")
    verifications: Mapped[list["Verification"]] = relationship(back_populates="incident")
    reports: Mapped[list["Report"]] = relationship(back_populates="incident")


class InvestigationRound(TimestampMixin, Base):
    """One independently traceable investigation execution within an Incident."""

    __tablename__ = "investigation_rounds"
    __table_args__ = (
        UniqueConstraint(
            "incident_id", "round_number", name="uq_investigation_rounds_incident_number"
        ),
        UniqueConstraint("id", "incident_id", name="uq_investigation_rounds_id_incident_id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    incident_id: Mapped[UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[InvestigationStatus] = mapped_column(INVESTIGATION_STATUS_TYPE, nullable=False)
    thread_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    terminal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    incident: Mapped[Incident] = relationship(back_populates="rounds")
    observations: Mapped[list["Observation"]] = relationship(
        back_populates="round", overlaps="incident,observations,round"
    )
    hypotheses: Mapped[list["Hypothesis"]] = relationship(
        back_populates="round", overlaps="hypotheses,incident,round"
    )
    evidence_items: Mapped[list["Evidence"]] = relationship(
        back_populates="round", overlaps="evidence_items,incident,round"
    )
    tool_calls: Mapped[list["ToolCall"]] = relationship(
        back_populates="round", overlaps="incident,round,tool_calls"
    )
    report: Mapped["Report | None"] = relationship(
        back_populates="round", uselist=False, overlaps="incident,report,reports"
    )


class Observation(Base):
    """User-supplied, unverified information that is intentionally distinct from Evidence."""

    __tablename__ = "observations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["round_id", "incident_id"],
            ["investigation_rounds.id", "investigation_rounds.incident_id"],
            name="observations_round_id_incident_id_fkey",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    incident_id: Mapped[UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    round_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    context_data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    incident: Mapped[Incident] = relationship(back_populates="observations", overlaps="round")
    round: Mapped[InvestigationRound | None] = relationship(
        back_populates="observations", overlaps="incident,observations"
    )


class IncidentRecordMixin:
    """Common foreign-key identity for audit records owned by an incident."""

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    incident_id: Mapped[UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )


class Hypothesis(TimestampMixin, IncidentRecordMixin, Base):
    __tablename__ = "hypotheses"
    __table_args__ = (
        ForeignKeyConstraint(
            ["round_id", "incident_id"],
            ["investigation_rounds.id", "investigation_rounds.incident_id"],
            name="hypotheses_round_id_incident_id_fkey",
            ondelete="RESTRICT",
        ),
    )

    summary: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="OPEN", nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    round_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True, index=True)

    incident: Mapped[Incident] = relationship(back_populates="hypotheses", overlaps="round")
    round: Mapped[InvestigationRound | None] = relationship(
        back_populates="hypotheses", overlaps="hypotheses,incident"
    )
    evidence_items: Mapped[list["Evidence"]] = relationship(back_populates="hypothesis")


class Evidence(TimestampMixin, IncidentRecordMixin, Base):
    __tablename__ = "evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["round_id", "incident_id"],
            ["investigation_rounds.id", "investigation_rounds.incident_id"],
            name="evidence_round_id_incident_id_fkey",
            ondelete="RESTRICT",
        ),
    )

    hypothesis_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("hypotheses.id", ondelete="SET NULL"), nullable=True, index=True
    )
    evidence_type: Mapped[str] = mapped_column(String(100), nullable=False)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    round_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True, index=True)

    incident: Mapped[Incident] = relationship(back_populates="evidence_items", overlaps="round")
    round: Mapped[InvestigationRound | None] = relationship(
        back_populates="evidence_items", overlaps="evidence_items,incident"
    )
    hypothesis: Mapped[Hypothesis | None] = relationship(back_populates="evidence_items")


class ToolCall(TimestampMixin, IncidentRecordMixin, Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        ForeignKeyConstraint(
            ["round_id", "incident_id"],
            ["investigation_rounds.id", "investigation_rounds.incident_id"],
            name="tool_calls_round_id_incident_id_fkey",
            ondelete="RESTRICT",
        ),
    )

    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    input_data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    round_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True, index=True)

    incident: Mapped[Incident] = relationship(back_populates="tool_calls", overlaps="round")
    round: Mapped[InvestigationRound | None] = relationship(
        back_populates="tool_calls", overlaps="incident,tool_calls"
    )


class Approval(TimestampMixin, IncidentRecordMixin, Base):
    __tablename__ = "approvals"
    __table_args__ = (UniqueConstraint("action_id", name="uq_approvals_action_id"),)

    action_id: Mapped[UUID] = mapped_column(
        ForeignKey("actions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(50), nullable=False)

    incident: Mapped[Incident] = relationship(back_populates="approvals")
    action: Mapped[Action] = relationship(back_populates="approvals")


class Action(TimestampMixin, IncidentRecordMixin, Base):
    __tablename__ = "actions"

    action_type: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    incident: Mapped[Incident] = relationship(back_populates="actions")
    approvals: Mapped[list[Approval]] = relationship(back_populates="action")
    verifications: Mapped[list["Verification"]] = relationship(back_populates="action")


class Verification(TimestampMixin, IncidentRecordMixin, Base):
    __tablename__ = "verifications"
    __table_args__ = (UniqueConstraint("action_id", name="uq_verifications_action_id"),)

    action_id: Mapped[UUID] = mapped_column(
        ForeignKey("actions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    incident: Mapped[Incident] = relationship(back_populates="verifications")
    action: Mapped[Action] = relationship(back_populates="verifications")


class Report(TimestampMixin, IncidentRecordMixin, Base):
    __tablename__ = "reports"
    __table_args__ = (
        UniqueConstraint("round_id", name="uq_reports_round_id"),
        UniqueConstraint("incident_id", "version", name="uq_reports_incident_version"),
        ForeignKeyConstraint(
            ["round_id", "incident_id"],
            ["investigation_rounds.id", "investigation_rounds.incident_id"],
            name="reports_round_id_incident_id_fkey",
            ondelete="RESTRICT",
        ),
    )

    round_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)

    incident: Mapped[Incident] = relationship(back_populates="reports", overlaps="round")
    round: Mapped[InvestigationRound] = relationship(
        back_populates="report", overlaps="incident,reports"
    )


class KnowledgeDocument(TimestampMixin, Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    source_path: Mapped[str] = mapped_column(String(500), unique=True, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    document_type: Mapped[str] = mapped_column(String(100), nullable=False)
    service: Mapped[str | None] = mapped_column(String(100), nullable=True)
    environment: Mapped[str | None] = mapped_column(String(50), nullable=True)
    metadata_data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    chunks: Mapped[list["KnowledgeChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class KnowledgeChunk(TimestampMixin, Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_knowledge_chunks_document_index"),
        Index(
            "ix_knowledge_chunks_text_search_vector",
            "text_search_vector",
            postgresql_using="gin",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(), nullable=True
    )
    text_search_vector: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english'::regconfig, content)", persisted=True),
        nullable=False,
    )

    document: Mapped[KnowledgeDocument] = relationship(back_populates="chunks")


def _pending_compatibility_target(session: Session) -> InvestigationTarget | None:
    """Return a compatibility target already staged in this unit of work, if any."""
    return next(
        (
            item
            for item in session.new
            if isinstance(item, InvestigationTarget)
            and item.slug == MIGRATION_COMPATIBILITY_TARGET_SLUG
        ),
        None,
    )


def _compatibility_target(session: Session) -> InvestigationTarget:
    """Load or stage the explicit V1 compatibility target used during the transition."""
    target = _pending_compatibility_target(session)
    if target is not None:
        return target
    target = session.scalar(
        select(InvestigationTarget).where(
            InvestigationTarget.slug == MIGRATION_COMPATIBILITY_TARGET_SLUG
        )
    )
    if target is not None:
        return target
    target = InvestigationTarget(
        name="Migration compatibility target (legacy V1 incidents)",
        slug=MIGRATION_COMPATIBILITY_TARGET_SLUG,
        description=(
            "Automatically assigned only to preserve V1 incident and workflow compatibility."
        ),
        environment="legacy-compatibility",
        enabled=True,
    )
    session.add(target)
    return target


def _compatibility_service(
    session: Session, target: InvestigationTarget, legacy_service: str
) -> Service:
    """Load or stage one target-scoped Service for a legacy free-form service value."""
    service = next(
        (
            item
            for item in session.new
            if isinstance(item, Service)
            and item.target is target
            and item.name == legacy_service
        ),
        None,
    )
    if service is not None:
        return service
    if target.id is not None:
        service = session.scalar(
            select(Service).where(Service.target_id == target.id, Service.name == legacy_service)
        )
        if service is not None:
            return service
    service = Service(
        name=legacy_service,
        display_name=legacy_service,
        description="Compatibility mapping for the legacy Incident.service field.",
        enabled=True,
    )
    service.target = target
    session.add(service)
    return service


def _compatibility_round(session: Session, incident_id: UUID) -> InvestigationRound | None:
    """Find the V1-owned round by the Incident's stable legacy workflow thread."""
    incident = session.get(Incident, incident_id)
    if incident is None:
        return None
    return session.scalar(
        select(InvestigationRound).where(
            InvestigationRound.incident_id == incident.id,
            InvestigationRound.thread_id == incident.thread_id,
        )
    )


@event.listens_for(Session, "before_flush")
def _preserve_v1_incident_compatibility(
    session: Session, _flush_context: object, _instances: object
) -> None:
    """Attach legacy Incident writes to a target, service, and first investigation round.

    The old workflow still owns Incident.thread_id.  This transition hook mirrors that stable
    identifier onto round 1 so old callers continue to work while new domain records are complete.
    """
    new_incidents = [item for item in session.new if isinstance(item, Incident)]
    for incident in new_incidents:
        if incident.investigation_status is None:
            incident.investigation_status = legacy_status_projection(incident.status or "OPEN")
        if incident.target is None and incident.target_id is None:
            target = _compatibility_target(session)
            incident.target = target
        else:
            target = incident.target
        if incident.service_record is None and incident.service_id is None:
            if target is None:
                raise ValueError("Incident service requires an InvestigationTarget")
            incident.service_record = _compatibility_service(session, target, incident.service)
        if not incident.rounds:
            incident.rounds.append(
                InvestigationRound(
                    round_number=1,
                    status=incident.investigation_status,
                    thread_id=incident.thread_id,
                )
            )

    for record_type in (Hypothesis, Evidence, ToolCall):
        for record in (item for item in session.new if isinstance(item, record_type)):
            if (
                record.round is not None
                or record.round_id is not None
                or record.incident_id is None
            ):
                continue
            round_record = _compatibility_round(session, record.incident_id)
            if round_record is not None:
                record.round = round_record

    for report in (item for item in session.new if isinstance(item, Report)):
        round_record = report.round
        if round_record is None and report.round_id is None and report.incident_id is not None:
            round_record = _compatibility_round(session, report.incident_id)
        if round_record is None:
            if report.round_id is None:
                raise ValueError("Report requires an InvestigationRound")
            continue
        if report.round is None:
            report.round = round_record
        if report.version is None:
            report.version = round_record.round_number


def _terminal_round_id(session: Session, round_id: UUID | None) -> UUID | None:
    """Return the id only when the persisted owning V2 round is terminal."""
    if round_id is None:
        return None
    status, legacy_status = session.execute(
        select(InvestigationRound.status, Incident.status)
        .join(Incident, Incident.id == InvestigationRound.incident_id)
        .where(InvestigationRound.id == round_id)
    ).one_or_none() or (None, None)
    if legacy_status in {"WAITING_APPROVAL", "REMEDIATING", "RESOLVED", "NEEDS_MANUAL_ACTION"}:
        return None
    return round_id if status in {
        InvestigationStatus.CONCLUDED,
        InvestigationStatus.INCONCLUSIVE,
        InvestigationStatus.FAILED,
    } else None


def _record_touches_terminal_round(session: Session, record: object) -> bool:
    """Detect writes to a terminal round, including attempts to move old data away."""
    state = inspect(record)
    round_ids = {getattr(record, "round_id", None)}
    history = state.attrs.round_id.history
    round_ids.update(history.deleted)
    return any(_terminal_round_id(session, round_id) is not None for round_id in round_ids)


@event.listens_for(Session, "before_flush")
def _enforce_terminal_round_immutability(
    session: Session, _flush_context: object, _instances: object
) -> None:
    """Preserve terminal round facts as immutable historical investigation snapshots."""
    for round_record in (item for item in session.dirty if isinstance(item, InvestigationRound)):
        if _terminal_round_id(session, round_record.id) is not None:
            raise ValueError("terminal InvestigationRound is immutable")

    for record in (
        item
        for item in session.new.union(session.dirty)
        if isinstance(item, (Hypothesis, Evidence, ToolCall))
    ):
        if _record_touches_terminal_round(session, record):
            raise ValueError("terminal InvestigationRound records are immutable")

    for report in (item for item in session.dirty if isinstance(item, Report)):
        content_history = inspect(report).attrs.content.history
        report_contents = [report.content, *content_history.deleted]
        if any(
            isinstance(content, dict) and content.get("schema_version") == "v2"
            for content in report_contents
        ):
            raise ValueError("Report snapshots are immutable")
