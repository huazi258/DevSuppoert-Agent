"""HTTP endpoints for the Incident Service."""

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from devsupport_backend.approvals import (
    ApprovalDecisionConflict,
    ApprovalResumeError,
    ApprovalService,
    ApprovalValidationError,
    ApprovalWorkflowCoordinator,
    PostgresApprovalWorkflowCoordinator,
    PostgresWorkflowStateReader,
    WorkflowStateReader,
)
from devsupport_backend.database import get_session
from devsupport_backend.models import Approval, Incident, Report
from devsupport_backend.schemas.approvals import ApprovalCreate, ApprovalResponse
from devsupport_backend.schemas.incidents import IncidentCreate, IncidentResponse, ReportResponse
from devsupport_backend.schemas.workflows import WorkflowResponse
from devsupport_backend.workflow_console import (
    PostgresWorkflowRuntime,
    WorkflowConflictError,
    WorkflowConsoleService,
    WorkflowNotStartedError,
    WorkflowRuntime,
    WorkflowStartError,
    WorkflowStateConflict,
)

router = APIRouter(prefix="/incidents", tags=["incidents"])
SessionDependency = Annotated[Session, Depends(get_session)]


def get_workflow_state_reader() -> WorkflowStateReader:
    """Build the read-only persisted-checkpoint boundary for Approval validation."""
    return PostgresWorkflowStateReader()


def get_approval_workflow_coordinator() -> ApprovalWorkflowCoordinator:
    """Build the same-thread PostgreSQL resume boundary for a persisted decision."""
    return PostgresApprovalWorkflowCoordinator()


def get_workflow_runtime(session: SessionDependency) -> WorkflowRuntime:
    """Build the production runtime using this request's authoritative DB session."""
    return PostgresWorkflowRuntime(session)


WorkflowStateReaderDependency = Annotated[WorkflowStateReader, Depends(get_workflow_state_reader)]
ApprovalWorkflowCoordinatorDependency = Annotated[
    ApprovalWorkflowCoordinator, Depends(get_approval_workflow_coordinator)
]
WorkflowRuntimeDependency = Annotated[WorkflowRuntime, Depends(get_workflow_runtime)]


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
        thread_id=str(uuid4()),
    )
    session.add(incident)
    session.commit()
    session.refresh(incident)
    return incident


@router.post("/{incident_id}/workflow", response_model=WorkflowResponse)
def start_workflow(
    incident_id: UUID,
    session: SessionDependency,
    workflow_runtime: WorkflowRuntimeDependency,
) -> WorkflowResponse:
    """Start one official persisted workflow for an otherwise OPEN Incident."""
    try:
        return WorkflowConsoleService(session, workflow_runtime).start(incident_id)
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except (WorkflowConflictError, WorkflowStateConflict) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except WorkflowStartError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error


@router.get("/{incident_id}/workflow", response_model=WorkflowResponse)
def get_workflow(
    incident_id: UUID,
    session: SessionDependency,
    workflow_runtime: WorkflowRuntimeDependency,
) -> WorkflowResponse:
    """Return a read-only, intentionally narrow projection of one workflow checkpoint."""
    try:
        return WorkflowConsoleService(session, workflow_runtime).read(incident_id)
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except WorkflowNotStartedError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except (WorkflowConflictError, WorkflowStateConflict) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.get("/{incident_id}", response_model=IncidentResponse)
def get_incident(incident_id: UUID, session: SessionDependency) -> Incident:
    """Return one incident or a standard not-found response."""
    incident = session.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found")
    return incident


@router.get("/{incident_id}/report", response_model=ReportResponse)
def get_final_report(incident_id: UUID, session: SessionDependency) -> Report:
    if session.get(Incident, incident_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found")
    report = session.scalar(select(Report).where(Report.incident_id == incident_id))
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Final report not found")
    return report


@router.get("", response_model=list[IncidentResponse])
def list_incidents(session: SessionDependency) -> list[Incident]:
    """Return all incidents newest first without adding search or pagination yet."""
    return list(session.scalars(select(Incident).order_by(Incident.created_at.desc())))


@router.post("/{incident_id}/approval", response_model=ApprovalResponse)
def record_approval(
    incident_id: UUID,
    payload: ApprovalCreate,
    session: SessionDependency,
    workflow_state_reader: WorkflowStateReaderDependency,
    workflow_coordinator: ApprovalWorkflowCoordinatorDependency,
) -> Approval:
    """Persist a decision, then wake only its existing interrupted workflow thread."""
    try:
        result = ApprovalService(session, workflow_state_reader).record_decision(
            incident_id, payload.decision
        )
        if result.resume_required:
            incident = session.get(Incident, incident_id)
            if incident is None or not incident.thread_id:
                raise ApprovalValidationError("Incident has no stable workflow thread")
            workflow_coordinator.resume(incident.thread_id)
        return result.approval
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except (ApprovalValidationError, ApprovalDecisionConflict) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ApprovalResumeError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error
