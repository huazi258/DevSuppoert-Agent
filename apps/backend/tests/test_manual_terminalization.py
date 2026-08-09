"""PostgreSQL terminalization is separate from final-report projection."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from devsupport_backend.agent.state import AgentStage, create_initial_agent_state
from devsupport_backend.agent.terminalization import (
    ManualTerminalizationError,
    PostgresManualTerminalizer,
)
from devsupport_backend.models import Incident


def _incident(session: Session) -> Incident:
    now = datetime.now(UTC)
    incident = Incident(
        service="order-service",
        environment="local",
        status="OPEN",
        description="Manual terminalization test.",
        time_range_start=now,
        time_range_end=now + timedelta(minutes=1),
        thread_id=str(uuid4()),
    )
    session.add(incident)
    session.commit()
    return incident


def test_postgres_terminalizer_persists_manual_status(database_session: Session) -> None:
    incident = _incident(database_session)
    state = create_initial_agent_state(incident)

    updated = PostgresManualTerminalizer(database_session).mark_needs_manual_action(state)

    database_session.refresh(incident)
    assert incident.status == "NEEDS_MANUAL_ACTION"
    assert updated["current_stage"] is AgentStage.NEEDS_MANUAL_ACTION
    assert updated["report_outcome"] is None


def test_postgres_terminalizer_rejects_checkpoint_incident_mismatch(
    database_session: Session,
) -> None:
    incident = _incident(database_session)
    state = create_initial_agent_state(incident)
    state["incident"] = state["incident"].model_copy(update={"service": "payment-service"})

    with pytest.raises(ManualTerminalizationError, match="binding mismatch"):
        PostgresManualTerminalizer(database_session).mark_needs_manual_action(state)
