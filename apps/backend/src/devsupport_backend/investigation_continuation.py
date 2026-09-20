"""Formal V2 continuation boundary for supplemental Incident observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from devsupport_backend.investigation_lifecycle import (
    InvestigationRoundService,
    current_round,
)
from devsupport_backend.investigation_status import InvestigationStatus
from devsupport_backend.models import Incident, Observation
from devsupport_backend.workflow_console import WorkflowConsoleService, WorkflowRuntime


class InvestigationContinuationError(ValueError):
    """Supplemental observation input is not suitable for a new V2 round."""


@dataclass(frozen=True)
class InvestigationContinuation:
    """Stable API-facing facts for one accepted next InvestigationRound."""

    incident_id: UUID
    round_id: UUID
    round_number: int
    status: InvestigationStatus
    observation_id: UUID
    observation_content: str
    observation_observed_at: datetime
    previous_round_id: UUID


class InvestigationContinuationService:
    """Create and accept one isolated V2 round from a terminal Incident only."""

    def __init__(self, session: Session, runtime: WorkflowRuntime) -> None:
        self._session = session
        self._runtime = runtime

    def continue_with_observation(
        self,
        incident_id: UUID,
        observation_content: str,
        *,
        observed_at: datetime | None = None,
    ) -> InvestigationContinuation:
        """Bind unverified supplemental input, then accept its fresh workflow thread."""
        content = observation_content.strip()
        if not content:
            raise InvestigationContinuationError("Supplemental observation must not be blank")
        if observed_at is not None and (
            observed_at.tzinfo is None or observed_at.utcoffset() is None
        ):
            raise InvestigationContinuationError(
                "Supplemental observation time must include a timezone"
            )
        incident = self._session.get(Incident, incident_id)
        if incident is None:
            raise LookupError("Incident not found")
        previous_round = current_round(self._session, incident.id)
        observation = Observation(
            content=content,
            observed_at=observed_at or datetime.now(UTC),
        )
        round_record = InvestigationRoundService(self._session).create_next_round(
            incident.id, observation
        )
        WorkflowConsoleService(self._session, self._runtime).accept_start(incident.id)
        self._session.refresh(round_record)
        self._session.refresh(observation)
        return InvestigationContinuation(
            incident_id=incident.id,
            round_id=round_record.id,
            round_number=round_record.round_number,
            status=round_record.status,
            observation_id=observation.id,
            observation_content=observation.content,
            observation_observed_at=observation.observed_at,
            previous_round_id=previous_round.id,
        )
