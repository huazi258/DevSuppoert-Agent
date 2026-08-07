"""Request and response schemas for the Incident API."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class IncidentCreate(BaseModel):
    """Input required to create an incident without starting a workflow."""

    service: str = Field(max_length=100)
    environment: str = Field(max_length=50)
    description: str = Field(max_length=10_000)
    time_range_start: datetime
    time_range_end: datetime

    @field_validator("service", "environment", "description")
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


class IncidentResponse(BaseModel):
    """Public representation of a persisted incident."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    service: str
    environment: str
    description: str
    status: str
    time_range_start: datetime
    time_range_end: datetime
    thread_id: str | None
    created_at: datetime
    updated_at: datetime
