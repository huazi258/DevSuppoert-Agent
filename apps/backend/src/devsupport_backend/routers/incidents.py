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
from devsupport_backend.config import settings
from devsupport_backend.database import SessionLocal, get_session
from devsupport_backend.investigation_continuation import (
    InvestigationContinuationError,
    InvestigationContinuationService,
)
from devsupport_backend.investigation_lifecycle import InvestigationLifecycleError
from devsupport_backend.investigation_status import InvestigationStatus
from devsupport_backend.models import Approval, Incident, InvestigationTarget, Report, Service
from devsupport_backend.schemas.approvals import ApprovalCreate, ApprovalResponse
from devsupport_backend.schemas.incidents import (
    IncidentCreate,
    IncidentResponse,
    InvestigationContinuationResponse,
    InvestigationServiceOptionResponse,
    InvestigationTargetOptionResponse,
    ObservationResponse,
    ReportResponse,
    SupplementalObservationCreate,
)
from devsupport_backend.schemas.workflows import (
    WorkflowProgressResponse,
    WorkflowResponse,
    WorkflowStartResponse,
    WorkflowTimelineResponse,
)
from devsupport_backend.target_config import (
    TargetCapability,
    TargetConfigError,
    TargetConfigRegistry,
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
# V1 remediation is retained for isolated compatibility applications only.
legacy_router = APIRouter(prefix="/legacy/incidents", tags=["legacy-remediation"])
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


def get_target_config_registry() -> TargetConfigRegistry:
    """Load deployment-owned target capabilities without exposing provider credentials."""
    return TargetConfigRegistry.from_settings(settings)


TargetConfigRegistryDependency = Annotated[
    TargetConfigRegistry, Depends(get_target_config_registry)
]


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


@router.get("/investigation-targets", response_model=list[InvestigationTargetOptionResponse])
def list_investigation_targets(
    session: SessionDependency,
    target_configs: TargetConfigRegistryDependency,
) -> list[InvestigationTargetOptionResponse]:
    """List only enabled, deployment-configured Target and Service selection metadata."""
    targets = session.scalars(
        select(InvestigationTarget)
        .where(InvestigationTarget.enabled.is_(True))
        .order_by(InvestigationTarget.name)
    )
    options: list[InvestigationTargetOptionResponse] = []
    for target in targets:
        try:
            target_config = target_configs.get(target_id=target.id, slug=target.slug)
        except TargetConfigError:
            # A database record without deployment configuration must not be selectable.
            continue
        if target_config.environment != target.environment:
            continue
        services = session.scalars(
            select(Service)
            .where(Service.target_id == target.id, Service.enabled.is_(True))
            .order_by(Service.display_name)
        )
        enabled_services = [
            InvestigationServiceOptionResponse(
                id=service.id,
                name=service.name,
                display_name=service.display_name,
            )
            for service in services
            if any(configured.name == service.name for configured in target_config.services)
        ]
        options.append(
            InvestigationTargetOptionResponse(
                id=target.id,
                display_name=target.name,
                environment=target.environment,
                capabilities=[
                    capability.value
                    for capability in TargetCapability
                    if target_config.capability(capability).enabled
                ],
                services=enabled_services,
            )
        )
    return options


@router.post("", response_model=IncidentResponse, status_code=status.HTTP_201_CREATED)
def create_incident(
    payload: IncidentCreate,
    session: SessionDependency,
    target_configs: TargetConfigRegistryDependency,
) -> Incident:
    """Persist a new OPEN incident without starting an investigation workflow."""
    target = session.get(InvestigationTarget, payload.target_id)
    service_record = session.scalar(
        select(Service).where(
            Service.id == payload.service_id,
            Service.target_id == payload.target_id,
        )
    )
    if target is None or service_record is None or not target.enabled or not service_record.enabled:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Unknown target or service",
        )
    try:
        target_config = target_configs.get(target_id=target.id, slug=target.slug)
        if target_config.environment != target.environment:
            raise TargetConfigError("target environment does not match deployment configuration")
        target_configs.require_service(service_record.name, target_id=target.id)
    except TargetConfigError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error
    incident = Incident(
        service=service_record.name,
        environment=target.environment,
        description=payload.description,
        time_range_start=payload.time_range_start,
        time_range_end=payload.time_range_end,
        status="OPEN",
        investigation_status=InvestigationStatus.OPEN,
        thread_id=str(uuid4()),
        target=target,
        service_record=service_record,
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


@router.post(
    "/{incident_id}/continuations",
    response_model=InvestigationContinuationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def continue_investigation(
    incident_id: UUID,
    payload: SupplementalObservationCreate,
    background_tasks: BackgroundTasks,
    session: SessionDependency,
    workflow_runtime: WorkflowRuntimeDependency,
) -> InvestigationContinuationResponse:
    """Accept a new read-only V2 round from one terminal Incident observation."""
    try:
        continuation = InvestigationContinuationService(
            session, workflow_runtime
        ).continue_with_observation(
            incident_id,
            payload.content,
            observed_at=payload.observed_at,
        )
        background_tasks.add_task(execute_accepted_start, incident_id)
        return InvestigationContinuationResponse(
            incident_id=continuation.incident_id,
            round_id=continuation.round_id,
            round_number=continuation.round_number,
            status=continuation.status,
            observation=ObservationResponse(
                id=continuation.observation_id,
                content=continuation.observation_content,
                observed_at=continuation.observation_observed_at,
            ),
            previous_round_id=continuation.previous_round_id,
        )
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except (
        InvestigationContinuationError,
        InvestigationLifecycleError,
        WorkflowConflictError,
        WorkflowStateConflict,
    ) as error:
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
    report = session.scalar(
        select(Report)
        .where(Report.incident_id == incident_id)
        .order_by(Report.version.desc())
        .limit(1)
    )
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Final report not found")
    return report


@router.get("", response_model=list[IncidentResponse])
def list_incidents(session: SessionDependency) -> list[Incident]:
    """Return all incidents newest first without adding search or pagination yet."""
    return list(session.scalars(select(Incident).order_by(Incident.created_at.desc())))


@legacy_router.post("/{incident_id}/approval", response_model=ApprovalResponse)
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
