"""HTTP endpoints for the Incident Service."""

import logging
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
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
from devsupport_backend.database import SessionLocal, get_session
from devsupport_backend.models import Approval, Incident, Report
from devsupport_backend.schemas.approvals import ApprovalCreate, ApprovalResponse
from devsupport_backend.schemas.incidents import IncidentCreate, IncidentResponse, ReportResponse
from devsupport_backend.schemas.workflows import (
    WorkflowProgressResponse,
    WorkflowResponse,
    WorkflowStartResponse,
    WorkflowTimelineResponse,
)
from devsupport_backend.workflow_console import (
    PostgresWorkflowRuntime,
    WorkflowConflictError,
    WorkflowConsoleService,
    WorkflowNotStartedError,
    WorkflowRetryError,
    WorkflowRuntime,
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


def execute_accepted_start(incident_id: UUID) -> None:
    """Run one accepted workflow in an in-process task with an independent DB Session."""
    try:
        with SessionLocal() as session:
            runtime = PostgresWorkflowRuntime(session)
            WorkflowConsoleService(session, runtime).execute_accepted_start(incident_id)
    except Exception:
        # Background failures have no HTTP response; retain safe server-side diagnostics only.
        logging.getLogger(__name__).exception(
            "Accepted workflow background execution failed",
            extra={"incident_id": str(incident_id)},
        )
        raise


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


@router.post(
    "/{incident_id}/workflow",
    response_model=WorkflowStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_workflow(
    incident_id: UUID,
    background_tasks: BackgroundTasks,
    session: SessionDependency,
) -> WorkflowStartResponse:
    """Accept one official workflow, then run it after the HTTP response is sent."""
    try:
        acknowledgement = WorkflowConsoleService(
            session, PostgresWorkflowRuntime(session)
        ).accept_start(incident_id)
        background_tasks.add_task(execute_accepted_start, incident_id)
        return acknowledgement
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except (WorkflowConflictError, WorkflowStateConflict) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post("/{incident_id}/workflow/retry", response_model=WorkflowResponse)
def retry_workflow(
    incident_id: UUID,
    session: SessionDependency,
    workflow_runtime: WorkflowRuntimeDependency,
) -> WorkflowResponse:
    """Retry only a freshly revalidated persisted pre-approval workflow failure."""
    try:
        return WorkflowConsoleService(session, workflow_runtime).retry(incident_id)
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except (WorkflowConflictError, WorkflowStateConflict) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except WorkflowRetryError as error:
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


@router.get("/{incident_id}/workflow/progress", response_model=WorkflowProgressResponse)
def get_workflow_progress(
    incident_id: UUID,
    session: SessionDependency,
    workflow_runtime: WorkflowRuntimeDependency,
) -> WorkflowProgressResponse:
    """Return the latest safe persisted progress, including accepted starts without checkpoints."""
    try:
        return WorkflowConsoleService(session, workflow_runtime).read_progress(incident_id)
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except (WorkflowConflictError, WorkflowStateConflict) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.get("/{incident_id}/workflow/timeline", response_model=WorkflowTimelineResponse)
def get_workflow_timeline(
    incident_id: UUID,
    session: SessionDependency,
    workflow_runtime: WorkflowRuntimeDependency,
) -> WorkflowTimelineResponse:
    """Return the stable user-facing narrative derived from persisted checkpoint history."""
    try:
        return WorkflowConsoleService(session, workflow_runtime).read_timeline(incident_id)
    except LookupError as error:
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
