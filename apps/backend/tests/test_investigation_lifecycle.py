"""M1.2 V2 Incident and InvestigationRound lifecycle contracts."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from devsupport_backend.investigation_lifecycle import (
    InvestigationLifecycleError,
    InvestigationLifecycleService,
)
from devsupport_backend.investigation_status import (
    TERMINAL_INVESTIGATION_STATUSES,
    InvestigationStatus,
    legacy_status_projection,
)
from devsupport_backend.models import Incident


def _incident(session: Session, *, legacy_status: str = "OPEN") -> Incident:
    incident = Incident(
        service="order-service",
        environment="test",
        status=legacy_status,
        description="V2 lifecycle test incident.",
        time_range_start=datetime(2026, 9, 19, 8, tzinfo=UTC),
        time_range_end=datetime(2026, 9, 19, 9, tzinfo=UTC),
        thread_id=str(uuid4()),
    )
    session.add(incident)
    session.commit()
    return incident


def test_v2_status_contract_contains_only_the_five_product_statuses() -> None:
    assert {status.value for status in InvestigationStatus} == {
        "OPEN",
        "INVESTIGATING",
        "CONCLUDED",
        "INCONCLUSIVE",
        "FAILED",
    }
    assert {"WAITING_APPROVAL", "REMEDIATING", "RESOLVED", "NEEDS_MANUAL_ACTION"}.isdisjoint(
        {status.value for status in InvestigationStatus}
    )


@pytest.mark.parametrize(
    "terminal_status",
    sorted(TERMINAL_INVESTIGATION_STATUSES, key=lambda status: status.value),
)
def test_open_round_starts_and_terminalizes_with_its_incident(
    database_session: Session, terminal_status: InvestigationStatus
) -> None:
    incident = _incident(database_session)
    round_record = incident.rounds[0]
    lifecycle = InvestigationLifecycleService(database_session)

    started = lifecycle.start(round_record.id)
    assert started.status is InvestigationStatus.INVESTIGATING
    database_session.refresh(incident)
    assert incident.investigation_status is InvestigationStatus.INVESTIGATING

    terminal = lifecycle.terminalize(
        round_record.id,
        terminal_status,
        terminal_reason="bounded lifecycle test",
    )
    database_session.refresh(incident)
    assert terminal.status is terminal_status
    assert terminal.completed_at is not None
    assert incident.investigation_status is terminal_status

    with pytest.raises(InvestigationLifecycleError):
        lifecycle.start(round_record.id)


def test_lifecycle_rejects_an_illegal_incident_and_round_status_pair(
    database_session: Session,
) -> None:
    incident = _incident(database_session)
    round_record = incident.rounds[0]
    incident.investigation_status = InvestigationStatus.INVESTIGATING
    database_session.commit()

    with pytest.raises(InvestigationLifecycleError):
        InvestigationLifecycleService(database_session).start(round_record.id)


@pytest.mark.parametrize(
    ("legacy_status", "expected"),
    [
        ("WAITING_APPROVAL", InvestigationStatus.INVESTIGATING),
        ("REMEDIATING", InvestigationStatus.INVESTIGATING),
        ("RESOLVED", InvestigationStatus.CONCLUDED),
        ("NEEDS_MANUAL_ACTION", InvestigationStatus.INCONCLUSIVE),
    ],
)
def test_legacy_statuses_are_projected_without_joining_the_v2_contract(
    database_session: Session, legacy_status: str, expected: InvestigationStatus
) -> None:
    incident = _incident(database_session, legacy_status=legacy_status)

    assert incident.status == legacy_status
    assert incident.investigation_status is expected
    assert incident.rounds[0].status is expected
    assert legacy_status_projection(legacy_status) is expected


def test_database_rejects_values_outside_the_v2_status_contract(database_session: Session) -> None:
    incident = _incident(database_session)
    round_record = incident.rounds[0]

    with pytest.raises(IntegrityError):
        database_session.execute(
            text("UPDATE incidents SET investigation_status = 'RESOLVED' WHERE id = :id"),
            {"id": incident.id},
        )
    database_session.rollback()

    with pytest.raises(IntegrityError):
        database_session.execute(
            text("UPDATE investigation_rounds SET status = 'WAITING_APPROVAL' WHERE id = :id"),
            {"id": round_record.id},
        )
    database_session.rollback()
