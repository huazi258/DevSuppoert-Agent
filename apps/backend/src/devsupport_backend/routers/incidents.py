"""HTTP endpoints for the Incident Service."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from devsupport_backend.database import get_session
from devsupport_backend.models import Incident
from devsupport_backend.schemas.incidents import IncidentCreate, IncidentResponse

router = APIRouter(prefix="/incidents", tags=["incidents"])
SessionDependency = Annotated[Session, Depends(get_session)]


@router.post("", response_model=IncidentResponse, status_code=status.HTTP_201_CREATED)
def create_incident(payload: IncidentCreate, session: SessionDependency) -> Incident:
    """Persist a new OPEN incident without starting an investigation workflow."""
    incident = Incident(
        service=payload.service,
        environment=payload.environment,
        description=payload.description,
        time_range_start=payload.time_range_start,
        time_range_end=payload.time_range_end,
        status="OPEN",
        thread_id=None,
    )
    session.add(incident)
    session.commit()
    session.refresh(incident)
    return incident


@router.get("/{incident_id}", response_model=IncidentResponse)
def get_incident(incident_id: UUID, session: SessionDependency) -> Incident:
    """Return one incident or a standard not-found response."""
    incident = session.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found")
    return incident


@router.get("", response_model=list[IncidentResponse])
def list_incidents(session: SessionDependency) -> list[Incident]:
    """Return all incidents newest first without adding search or pagination yet."""
    return list(session.scalars(select(Incident).order_by(Incident.created_at.desc())))
