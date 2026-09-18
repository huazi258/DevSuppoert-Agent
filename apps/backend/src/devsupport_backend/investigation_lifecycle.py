"""Deterministic V2 InvestigationRound lifecycle transitions."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from devsupport_backend.investigation_status import (
    TERMINAL_INVESTIGATION_STATUSES,
    InvestigationStatus,
)
from devsupport_backend.models import Incident, InvestigationRound, Observation


class InvestigationLifecycleError(ValueError):
    """A requested V2 lifecycle transition violates the persisted state contract."""


class InvestigationRoundCreationError(InvestigationLifecycleError):
    """A new round was requested outside the terminal-round creation boundary."""


def current_round(
    session: Session, incident_id: UUID, *, lock: bool = False
) -> InvestigationRound:
    """Return the latest persisted round; V2 never derives this from Incident.thread_id."""
    query = (
        select(InvestigationRound)
        .where(InvestigationRound.incident_id == incident_id)
        .order_by(InvestigationRound.round_number.desc())
        .limit(1)
    )
    if lock:
        query = query.with_for_update()
    round_record = session.scalar(query)
    if round_record is None:
        raise InvestigationLifecycleError("Incident has no InvestigationRound")
    return round_record


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
        if current_round(self._session, incident.id, lock=True).id != round_record.id:
            raise InvestigationLifecycleError("Only the current InvestigationRound can transition")
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


class InvestigationRoundService:
    """Create a new immutable V2 round only from a terminal Incident."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_next_round(self, incident_id: UUID, observation: Observation) -> InvestigationRound:
        """Bind one newly supplied observation to the next independently traceable round."""
        incident = self._session.scalar(
            select(Incident).where(Incident.id == incident_id).with_for_update()
        )
        if incident is None:
            raise LookupError("Incident not found")
        previous_round = current_round(self._session, incident.id, lock=True)
        if (
            incident.investigation_status not in TERMINAL_INVESTIGATION_STATUSES
            or previous_round.status not in TERMINAL_INVESTIGATION_STATUSES
        ):
            raise InvestigationRoundCreationError(
                "A new InvestigationRound requires a terminal Incident"
            )
        if (
            observation.id is not None
            and self._session.get(Observation, observation.id) is not None
        ):
            raise InvestigationRoundCreationError("A triggering Observation must be new")
        if observation.incident_id not in {None, incident.id}:
            raise InvestigationRoundCreationError("Observation belongs to another Incident")
        if observation.round_id is not None or observation.round is not None:
            raise InvestigationRoundCreationError("Observation is already bound to a round")

        round_record = InvestigationRound(
            incident=incident,
            round_number=previous_round.round_number + 1,
            status=InvestigationStatus.OPEN,
            thread_id=str(uuid4()),
        )
        observation.incident = incident
        observation.round = round_record
        incident.investigation_status = InvestigationStatus.OPEN
        # V1's display projection remains isolated from V2 lifecycle ownership.
        incident.status = "OPEN"
        self._session.add_all((round_record, observation))
        self._session.commit()
        self._session.refresh(round_record)
        return round_record
