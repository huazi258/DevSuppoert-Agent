"""Request and response schemas for the Incident API."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from devsupport_backend.investigation_status import InvestigationStatus


class IncidentCreate(BaseModel):
    """Input required to create an incident without starting a workflow."""

    target_id: UUID
    service_id: UUID
    description: str = Field(max_length=10_000)
    time_range_start: datetime
    time_range_end: datetime

    @field_validator("description")
    @classmethod
    def require_non_blank_text(cls, value: str) -> str:
        """Normalize required text and reject whitespace-only values."""
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("time_range_start", "time_range_end")
    @classmethod
    def require_timezone_aware_time(cls, value: datetime) -> datetime:
        """Keep API timestamps aligned with the timezone-aware database columns."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_time_range(self) -> "IncidentCreate":
        """Reject incident windows whose end precedes their start."""
        if self.time_range_start > self.time_range_end:
            raise ValueError("time_range_start must be before or equal to time_range_end")
        return self


class InvestigationServiceOptionResponse(BaseModel):
    """Safe service projection used by the Incident creation form."""

    id: UUID
    name: str
    display_name: str


class InvestigationTargetOptionResponse(BaseModel):
    """Safe deployment-owned target projection used by the Incident creation form."""

    id: UUID
    display_name: str
    environment: str
    capabilities: list[str]
    services: list[InvestigationServiceOptionResponse]


class IncidentResponse(BaseModel):
    """Public representation of a persisted incident."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    target_id: UUID
    service_id: UUID
    service: str
    environment: str
    description: str
    status: str
    investigation_status: InvestigationStatus
    time_range_start: datetime
    time_range_end: datetime
    thread_id: str
    created_at: datetime
    updated_at: datetime


class SupplementalObservationCreate(BaseModel):
    """Unverified user input that starts a new round only after terminalization."""

    content: str = Field(max_length=10_000)
    observed_at: datetime | None = None

    @field_validator("content")
    @classmethod
    def require_non_blank_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("observed_at")
    @classmethod
    def require_timezone_aware_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("must include a timezone")
        return value


class ObservationResponse(BaseModel):
    """Safe persisted representation of one supplemental observation."""

    id: UUID
    content: str
    observed_at: datetime


class InvestigationContinuationResponse(BaseModel):
    """Acknowledgement for a newly accepted, independently traceable V2 round."""

    incident_id: UUID
    round_id: UUID
    round_number: int
    status: InvestigationStatus
    observation: ObservationResponse
    previous_round_id: UUID
    accepted: bool = True


class InvestigationRoundReportSummaryResponse(BaseModel):
    """Safe report metadata attached to one immutable investigation round."""

    id: UUID
    conclusion_summary: str | None = None
    final_status: InvestigationStatus | None = None


class InvestigationRoundResponse(BaseModel):
    """Read-only, user-facing projection of one V2 investigation round."""

    round_id: UUID
    round_number: int
    status: InvestigationStatus
    thread_id: str
    started_at: datetime
    completed_at: datetime | None = None
    terminal_reason: str | None = None
    triggering_observation: ObservationResponse | None = None
    report: InvestigationRoundReportSummaryResponse | None = None
    is_current: bool


class ReportResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    incident_id: UUID
    root_cause: str | None
    content: dict[str, object]
    created_at: datetime
    updated_at: datetime
