"""Strict public workflow projections for the V0 Web Console."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, JsonValue

from devsupport_backend.agent.state import AgentStage, FailureCategory, TerminalReason

WorkflowProgressPhase = Literal[
    "not_started",
    "accepted",
    "running",
    "failed",
    "waiting_approval",
    "completed",
]

InvestigationTimelineEventType = Literal[
    "investigation_started",
    "knowledge_searched",
    "hypothesis_created",
    "evidence_collected",
    "hypothesis_updated",
    "conclusion_reached",
    "action_proposed",
    "policy_decision",
    "approval_wait",
    "approval_decision",
    "action_execution",
    "recovery_verification",
    "investigation_interrupted",
    "investigation_completed",
]


class WorkflowResponseModel(BaseModel):
    """Forbid accidental leakage of internal checkpoint fields."""

    model_config = ConfigDict(extra="forbid")


class WorkflowStartResponse(WorkflowResponseModel):
    """Narrow acknowledgement that a new investigation has been accepted."""

    incident_id: UUID
    incident_status: str
    accepted: bool = True


class WorkflowProgressLatestToolResponse(WorkflowResponseModel):
    """Small persisted summary of the latest completed or unavailable Tool call."""

    tool_name: str
    status: str
    duration_ms: float | None


class WorkflowProgressFailureResponse(WorkflowResponseModel):
    """Safe persisted workflow task failure facts for polling clients."""

    failed_node: str
    category: FailureCategory
    message: str
    retryable: bool


class WorkflowProgressResponse(WorkflowResponseModel):
    """Read-only persisted investigation progress without a synthetic completion estimate."""

    incident_id: UUID
    incident_status: str
    phase: WorkflowProgressPhase
    checkpoint_available: bool
    current_stage: AgentStage | None
    current_goal: str | None
    pending_tool_name: str | None
    hypothesis_count: int
    evidence_count: int
    tool_call_count: int
    investigation_round: int
    llm_call_count: int
    workflow_retry_count: int
    latest_tool: WorkflowProgressLatestToolResponse | None
    failure: WorkflowProgressFailureResponse | None
    terminal_reason: TerminalReason | None
    retry_available: bool = False


class InvestigationTimelineEventResponse(WorkflowResponseModel):
    """One stable user-facing fact derived from persisted investigation history."""

    event_id: str
    sequence: int
    event_type: InvestigationTimelineEventType
    occurred_at: datetime | None
    title: str
    summary: str
    status: str | None = None


class WorkflowTimelineResponse(WorkflowResponseModel):
    """Bounded read-only investigation narrative derived from checkpoint history."""

    incident_id: UUID
    checkpoint_available: bool
    truncated: bool = False
    events: list[InvestigationTimelineEventResponse]


class WorkflowHypothesisResponse(WorkflowResponseModel):
    id: UUID
    summary: str
    status: str
    confidence: float | None
    supporting_evidence_ids: list[UUID]
    contradicting_evidence_ids: list[UUID]
    next_check: str | None


class WorkflowEvidenceCitationResponse(WorkflowResponseModel):
    id: str
    document_id: UUID
    chunk_id: UUID
    source: str
    section: str
    document_reference: str


class WorkflowEvidenceResponse(WorkflowResponseModel):
    id: UUID
    evidence_type: str
    source: str
    summary: str
    reference: str | None
    citation: WorkflowEvidenceCitationResponse | None = None


class WorkflowToolErrorResponse(WorkflowResponseModel):
    code: str
    message: str
    retryable: bool


class WorkflowToolHistoryResponse(WorkflowResponseModel):
    tool_name: str
    tool_arguments: dict[str, JsonValue]
    status: str
    duration_ms: float | None
    evidence_ids: list[UUID]
    error: WorkflowToolErrorResponse | None


class WorkflowFinalConclusionResponse(WorkflowResponseModel):
    summary: str
    root_cause: str | None
    confidence: float | None
    supporting_evidence_ids: list[UUID]
    contradicting_evidence_ids: list[UUID]
    recommended_next_action: str | None


class WorkflowProposedActionResponse(WorkflowResponseModel):
    action_type: str
    summary: str
    reason: str
    risk: str
    supporting_evidence_ids: list[UUID]


class WorkflowPolicyResponse(WorkflowResponseModel):
    decision: str
    reason_code: str
    reason: str
    action_id: UUID | None


class WorkflowActionParametersResponse(WorkflowResponseModel):
    service: str
    environment: str
    current_version: str
    target_version: str
    reason: str


class WorkflowActionResponse(WorkflowResponseModel):
    action_id: UUID
    action_type: str
    status: str
    parameters: WorkflowActionParametersResponse
    executed_at: datetime | None


class WorkflowApprovalResponse(WorkflowResponseModel):
    approval_id: UUID
    action_id: UUID
    status: str


class WorkflowExecutionResponse(WorkflowResponseModel):
    action_id: UUID | None
    approval_id: UUID | None
    status: str
    service: str | None
    environment: str | None
    target_version: str | None
    executed: bool


class WorkflowVerificationResponse(WorkflowResponseModel):
    verification_id: UUID | None
    action_id: UUID | None
    status: str
    summary: str


class WorkflowReportOutcomeResponse(WorkflowResponseModel):
    report_id: UUID
    incident_id: UUID
    final_status: str


class WorkflowResponse(WorkflowResponseModel):
    incident_id: UUID
    incident_status: str
    current_stage: AgentStage
    hypotheses: list[WorkflowHypothesisResponse]
    evidence: list[WorkflowEvidenceResponse]
    tool_history: list[WorkflowToolHistoryResponse]
    current_goal: str | None
    final_conclusion: WorkflowFinalConclusionResponse | None
    proposed_action: WorkflowProposedActionResponse | None
    policy_outcome: WorkflowPolicyResponse | None
    action: WorkflowActionResponse | None
    approval_outcome: WorkflowApprovalResponse | None
    execution_outcome: WorkflowExecutionResponse | None
    verification_outcome: WorkflowVerificationResponse | None
    report_outcome: WorkflowReportOutcomeResponse | None
    terminal_reason: TerminalReason | None
    retry_available: bool = False
