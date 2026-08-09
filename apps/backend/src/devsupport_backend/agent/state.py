"""Structured, checkpoint-safe runtime state for future Agent workflow nodes.

These contracts describe transient investigation context only.  SQLAlchemy domain
models remain the product's persistent source of truth and are deliberately not
stored in this state.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Protocol, TypedDict
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from devsupport_backend.tools.registry import ToolName
from devsupport_backend.tools.schemas import ToolError, ToolStatus

MAX_EVIDENCE_DATA_SERIALIZED_BYTES = 16_000
"""Maximum UTF-8 JSON size retained for one concise evidence data payload."""


class AgentStage(StrEnum):
    """Named workflow stages; this task defines them but does not run a graph."""

    INTAKE = "intake"
    RETRIEVAL = "retrieval"
    HYPOTHESIS_GENERATION = "hypothesis_generation"
    INVESTIGATION_PLANNING = "investigation_planning"
    TOOL_EXECUTION = "tool_execution"
    HYPOTHESIS_UPDATE = "hypothesis_update"
    EVIDENCE_EVALUATION = "evidence_evaluation"
    RESOLUTION_PROPOSAL = "resolution_proposal"
    CONCLUSION = "conclusion"
    POLICY_GATE = "policy_gate"


class HypothesisStatus(StrEnum):
    """The only statuses allowed for a runtime investigation hypothesis."""

    ACTIVE = "ACTIVE"
    SUPPORTED = "SUPPORTED"
    REJECTED = "REJECTED"
    CONFIRMED = "CONFIRMED"


class EvaluationDecision(StrEnum):
    """Bounded outcomes of the later evidence-evaluation step."""

    CONTINUE = "CONTINUE"
    CONCLUDE = "CONCLUDE"
    NEEDS_MANUAL_ACTION = "NEEDS_MANUAL_ACTION"


class IntakeDecision(StrEnum):
    """Outcome of Intake, kept separate from later evidence evaluation."""

    READY = "READY"
    NEEDS_INFORMATION = "NEEDS_INFORMATION"


class ActionType(StrEnum):
    """The only high-level remediation recommendations recognized in V0."""

    ROLLBACK_DEPLOYMENT = "rollback_deployment"
    MANUAL_ACTION = "manual_action"


class PolicyDecision(StrEnum):
    """Bounded code-level decisions made by the Task 4.1 Policy Gate."""

    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    DENIED = "DENIED"


class PolicyReasonCode(StrEnum):
    """Auditable reasons for an allowlist-bound Policy Gate outcome."""

    APPROVAL_REQUIRED = "approval_required"
    INVESTIGATION_NOT_CONCLUDED = "investigation_not_concluded"
    MISSING_FINAL_CONCLUSION = "missing_final_conclusion"
    MISSING_PROPOSED_ACTION = "missing_proposed_action"
    MANUAL_ACTION = "manual_action"
    UNSUPPORTED_ACTION = "unsupported_action"
    PROPOSAL_PARAMETERS_NOT_EMPTY = "proposal_parameters_not_empty"
    PRODUCTION_ENVIRONMENT = "production_environment"
    UNSUPPORTED_ENVIRONMENT = "unsupported_environment"
    UNSUPPORTED_SERVICE = "unsupported_service"
    DEPLOYMENT_UNAVAILABLE = "deployment_unavailable"
    INVALID_DEPLOYMENT_STATE = "invalid_deployment_state"
    CONFLICTING_PENDING_ACTION = "conflicting_pending_action"


class IncidentStateSource(Protocol):
    """Minimum Incident projection accepted by the runtime state factory."""

    id: UUID
    service: str
    environment: str
    description: str
    time_range_start: datetime
    time_range_end: datetime


class StateModel(BaseModel):
    """Strict, small Pydantic models kept safe for a future checkpointer."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class IncidentContext(StateModel):
    """Normalized incident facts carried between workflow nodes."""

    id: UUID
    service: str = Field(min_length=1, max_length=100)
    environment: str = Field(min_length=1, max_length=50)
    description: str = Field(min_length=1, max_length=10_000)
    time_range_start: datetime
    time_range_end: datetime
    symptoms: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("time_range_start", "time_range_end")
    @classmethod
    def require_timezone_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("must include a timezone")
        return value

    @field_validator("symptoms")
    @classmethod
    def validate_symptoms(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("symptoms must not contain blank values")
        if any(len(value) > 500 for value in normalized):
            raise ValueError("each symptom must be at most 500 characters")
        return normalized

    @model_validator(mode="after")
    def validate_time_range(self) -> "IncidentContext":
        if self.time_range_start > self.time_range_end:
            raise ValueError("time_range_start must be before or equal to time_range_end")
        return self


class IntakeOutcome(StateModel):
    """Validated Intake result for later nodes to place into AgentState."""

    decision: IntakeDecision
    missing_information: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("missing_information")
    @classmethod
    def validate_missing_information(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("missing_information must not contain blank values")
        if any(len(value) > 500 for value in normalized):
            raise ValueError("each missing_information item must be at most 500 characters")
        return normalized


class HypothesisContext(StateModel):
    """A candidate explanation and the evidence references for and against it."""

    id: UUID = Field(default_factory=uuid4)
    summary: str = Field(min_length=1, max_length=2_000)
    status: HypothesisStatus = HypothesisStatus.ACTIVE
    confidence: float | None = Field(default=None, ge=0, le=1)
    supporting_evidence_ids: list[UUID] = Field(default_factory=list, max_length=100)
    contradicting_evidence_ids: list[UUID] = Field(default_factory=list, max_length=100)
    next_check: str | None = Field(default=None, min_length=1, max_length=1_000)


class EvidenceContext(StateModel):
    """A concise evidence fact, never an unbounded raw tool payload."""

    id: UUID = Field(default_factory=uuid4)
    evidence_type: str = Field(min_length=1, max_length=100)
    source: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=2_000)
    data: dict[str, JsonValue] = Field(default_factory=dict, max_length=50)
    reference: str | None = Field(default=None, min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def validate_data_size(self) -> "EvidenceContext":
        serialized_data = json.dumps(
            self.data,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        if len(serialized_data) > MAX_EVIDENCE_DATA_SERIALIZED_BYTES:
            raise ValueError(
                "evidence data exceeds "
                f"{MAX_EVIDENCE_DATA_SERIALIZED_BYTES} serialized bytes"
            )
        return self


class PendingToolCall(StateModel):
    """One planner-approved call constrained to the existing fixed Tool registry."""

    investigation_goal: str = Field(min_length=1, max_length=1_000)
    tool_name: ToolName
    tool_arguments: dict[str, JsonValue] = Field(default_factory=dict, max_length=50)
    reason: str = Field(min_length=1, max_length=2_000)


class ToolHistoryEntry(StateModel):
    """Compact audit context referencing derived evidence instead of raw results."""

    tool_name: ToolName
    tool_arguments: dict[str, JsonValue] = Field(default_factory=dict, max_length=50)
    status: ToolStatus
    duration_ms: float | None = Field(default=None, ge=0)
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=100)
    error: ToolError | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "ToolHistoryEntry":
        if self.status is ToolStatus.SUCCESS and self.error is not None:
            raise ValueError("successful tool history must not contain an error")
        if self.status is not ToolStatus.SUCCESS and self.error is None:
            raise ValueError("unsuccessful tool history must include an error")
        return self


class ProposedAction(StateModel):
    """A future resolution proposal only; this state does not authorize execution."""

    action_type: ActionType
    summary: str = Field(min_length=1, max_length=2_000)
    parameters: dict[str, JsonValue] = Field(default_factory=dict, max_length=50)
    reason: str = Field(min_length=1, max_length=2_000)
    risk: str = Field(min_length=1, max_length=1_000)
    supporting_evidence_ids: list[UUID] = Field(default_factory=list, max_length=100)


class FinalConclusion(StateModel):
    """Structured end-of-investigation conclusion tied to explicit evidence IDs."""

    summary: str = Field(min_length=1, max_length=2_000)
    root_cause: str | None = Field(default=None, min_length=1, max_length=2_000)
    confidence: float | None = Field(default=None, ge=0, le=1)
    supporting_evidence_ids: list[UUID] = Field(default_factory=list, max_length=100)
    contradicting_evidence_ids: list[UUID] = Field(default_factory=list, max_length=100)
    recommended_next_action: str | None = Field(default=None, min_length=1, max_length=2_000)


class PolicyOutcome(StateModel):
    """Checkpoint-safe result of code-level Policy Gate evaluation only."""

    decision: PolicyDecision
    reason_code: PolicyReasonCode
    reason: str = Field(min_length=1, max_length=2_000)
    action_id: UUID | None = None


class AgentState(TypedDict):
    """Full runtime state carried by future workflow nodes and checkpointed safely."""

    incident: IncidentContext
    current_stage: AgentStage
    hypotheses: list[HypothesisContext]
    evidence: list[EvidenceContext]
    current_goal: str | None
    pending_tool_call: PendingToolCall | None
    tool_history: list[ToolHistoryEntry]
    investigation_round: int
    tool_call_count: int
    intake_decision: IntakeDecision | None
    missing_information: list[str]
    evaluation_decision: EvaluationDecision | None
    proposed_action: ProposedAction | None
    final_conclusion: FinalConclusion | None
    policy_outcome: PolicyOutcome | None


def create_initial_agent_state(
    incident: IncidentStateSource, *, symptoms: list[str] | None = None
) -> AgentState:
    """Create the neutral intake state from an ORM or API Incident projection."""
    return {
        "incident": IncidentContext(
            id=incident.id,
            service=incident.service,
            environment=incident.environment,
            description=incident.description,
            time_range_start=incident.time_range_start,
            time_range_end=incident.time_range_end,
            symptoms=symptoms or [],
        ),
        "current_stage": AgentStage.INTAKE,
        "hypotheses": [],
        "evidence": [],
        "current_goal": None,
        "pending_tool_call": None,
        "tool_history": [],
        "investigation_round": 0,
        "tool_call_count": 0,
        "intake_decision": None,
        "missing_information": [],
        "evaluation_decision": None,
        "proposed_action": None,
        "final_conclusion": None,
        "policy_outcome": None,
    }


def agent_state_to_checkpoint_payload(state: AgentState) -> dict[str, object]:
    """Convert runtime models to JSON-compatible primitives for a future checkpointer."""
    return {
        "incident": state["incident"].model_dump(mode="json"),
        "current_stage": state["current_stage"].value,
        "hypotheses": [item.model_dump(mode="json") for item in state["hypotheses"]],
        "evidence": [item.model_dump(mode="json") for item in state["evidence"]],
        "current_goal": state["current_goal"],
        "pending_tool_call": (
            state["pending_tool_call"].model_dump(mode="json")
            if state["pending_tool_call"] is not None
            else None
        ),
        "tool_history": [item.model_dump(mode="json") for item in state["tool_history"]],
        "investigation_round": state["investigation_round"],
        "tool_call_count": state["tool_call_count"],
        "intake_decision": (
            state["intake_decision"].value if state["intake_decision"] is not None else None
        ),
        "missing_information": state["missing_information"],
        "evaluation_decision": (
            state["evaluation_decision"].value
            if state["evaluation_decision"] is not None
            else None
        ),
        "proposed_action": (
            state["proposed_action"].model_dump(mode="json")
            if state["proposed_action"] is not None
            else None
        ),
        "final_conclusion": (
            state["final_conclusion"].model_dump(mode="json")
            if state["final_conclusion"] is not None
            else None
        ),
        "policy_outcome": (
            state["policy_outcome"].model_dump(mode="json")
            if state["policy_outcome"] is not None
            else None
        ),
    }
