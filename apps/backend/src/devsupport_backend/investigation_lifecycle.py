"""Deterministic V2 InvestigationRound lifecycle transitions."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from devsupport_backend.investigation_status import (
    TERMINAL_INVESTIGATION_STATUSES,
    InvestigationStatus,
)
from devsupport_backend.models import Incident, InvestigationRound


class InvestigationLifecycleError(ValueError):
    """A requested V2 lifecycle transition violates the persisted state contract."""


class InvestigationLifecycleService:
    """Coordinate one V2 Round and its Incident's current status projection."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def start(self, round_id: UUID) -> InvestigationRound:
        """Move an OPEN round and its OPEN Incident into INVESTIGATING."""
        round_record, incident = self._locked_round_and_incident(round_id)
        self._require_current_pair(round_record, incident, InvestigationStatus.OPEN)
        round_record.status = InvestigationStatus.INVESTIGATING
        incident.investigation_status = InvestigationStatus.INVESTIGATING
        self._session.commit()
        self._session.refresh(round_record)
        return round_record

    def terminalize(
        self,
        round_id: UUID,
        status: InvestigationStatus,
        *,
        terminal_reason: str | None = None,
    ) -> InvestigationRound:
        """End an active round with a supported conclusion, inconclusive result, or failure."""
        if status not in TERMINAL_INVESTIGATION_STATUSES:
            raise InvestigationLifecycleError("V2 rounds can only terminalize to a terminal status")
        round_record, incident = self._locked_round_and_incident(round_id)
        self._require_current_pair(round_record, incident, InvestigationStatus.INVESTIGATING)
        round_record.status = status
        round_record.completed_at = datetime.now(UTC)
        round_record.terminal_reason = terminal_reason
        incident.investigation_status = status
        self._session.commit()
        self._session.refresh(round_record)
        return round_record

    def _locked_round_and_incident(self, round_id: UUID) -> tuple[InvestigationRound, Incident]:
        round_record = self._session.scalar(
            select(InvestigationRound)
            .where(InvestigationRound.id == round_id)
            .with_for_update()
        )
        if round_record is None:
            raise LookupError("InvestigationRound not found")
        incident = self._session.scalar(
            select(Incident).where(Incident.id == round_record.incident_id).with_for_update()
        )
        if incident is None:
            raise InvestigationLifecycleError("InvestigationRound Incident is missing")
        return round_record, incident

    @staticmethod
    def _require_current_pair(
        round_record: InvestigationRound,
        incident: Incident,
        expected_status: InvestigationStatus,
    ) -> None:
        if (
            round_record.status != expected_status
            or incident.investigation_status != expected_status
        ):
            raise InvestigationLifecycleError(
                "Incident and InvestigationRound cannot make the requested V2 lifecycle transition"
            )
