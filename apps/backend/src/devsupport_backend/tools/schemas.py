"""Pydantic contracts for every tool allowed in the V0 registry."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ToolStatus(StrEnum):
    """A tool outcome that can be persisted in later ToolCall audit records."""

    SUCCESS = "success"
    FAILURE = "failure"
    UNAVAILABLE = "unavailable"


class ToolInput(BaseModel):
    """Common strict boundary for all explicit tool inputs."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ToolError(BaseModel):
    """Safe, structured error information returned by a tool."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=1_000)
    retryable: bool = False


class ToolOutput(BaseModel):
    """Common strict outcome and timing boundary for all tool outputs."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: ToolStatus
    error: ToolError | None = None
    duration_ms: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_outcome(self) -> "ToolOutput":
        """Require errors for unsuccessful outcomes and reject them on success."""
        if self.status is ToolStatus.SUCCESS and self.error is not None:
            raise ValueError("successful tool output must not contain an error")
        if self.status is not ToolStatus.SUCCESS and self.error is None:
            raise ValueError("unsuccessful tool output must include an error")
        return self


class SearchKnowledgeInput(ToolInput):
    """Structured query and optional metadata filters for knowledge retrieval."""

    query: str = Field(min_length=1, max_length=2_000)
    service: str | None = Field(default=None, min_length=1, max_length=100)
    environment: str | None = Field(default=None, min_length=1, max_length=50)
    document_type: str | None = Field(default=None, min_length=1, max_length=100)
    top_k: int = Field(default=5, ge=1, le=20)


class CitationOutput(BaseModel):
    """Traceable RAG citation returned as a stable structured value."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    document_id: UUID
    chunk_id: UUID
    source: str = Field(min_length=1)
    section: str = Field(min_length=1)
    document_reference: str = Field(min_length=1)


class SearchKnowledgeResult(BaseModel):
    """RAG result retaining retrieval scores and all citation metadata."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: UUID
    document_id: UUID
    content: str = Field(min_length=1)
    service: str | None
    environment: str | None
    document_type: str = Field(min_length=1)
    source: str = Field(min_length=1)
    section: str = Field(min_length=1)
    vector_score: float | None
    keyword_score: float | None
    fusion_score: float = Field(ge=0)
    citation: CitationOutput


class SearchKnowledgeOutput(ToolOutput):
    """Outcome of searching the persisted knowledge corpus."""

    results: list[SearchKnowledgeResult] = Field(default_factory=list)


class TimeRangeToolInput(ToolInput):
    """Base for read-only runtime tools constrained to a time range."""

    service: str = Field(min_length=1, max_length=100)
    environment: str = Field(min_length=1, max_length=50)
    time_range_start: datetime
    time_range_end: datetime

    @model_validator(mode="after")
    def validate_time_range(self) -> "TimeRangeToolInput":
        """Reject inverted time ranges before an adapter can be called."""
        if self.time_range_start > self.time_range_end:
            raise ValueError("time_range_start must be before or equal to time_range_end")
        return self


class QueryLogsInput(TimeRangeToolInput):
    """Bounded query for structured runtime logs."""

    level: str | None = Field(default=None, min_length=1, max_length=20)
    query: str | None = Field(default=None, min_length=1, max_length=1_000)
    limit: int = Field(default=20, ge=1, le=100)


class ErrorPattern(BaseModel):
    """Aggregated log pattern rather than unbounded raw log output."""

    model_config = ConfigDict(extra="forbid")

    pattern: str = Field(min_length=1)
    count: int = Field(ge=1)


class LogSample(BaseModel):
    """One bounded representative log record."""

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    service: str = Field(min_length=1)
    level: str = Field(min_length=1)
    message: str = Field(min_length=1)
    request_id: str | None = None
    trace_id: str | None = None
    error_type: str | None = None
    status_code: int | None = None
    duration_ms: float | None = Field(default=None, ge=0)
    downstream_service: str | None = None


class QueryLogsOutput(ToolOutput):
    """Structured, bounded result from a logs adapter."""

    match_count: int = Field(default=0, ge=0)
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    error_patterns: list[ErrorPattern] = Field(default_factory=list)
    samples: list[LogSample] = Field(default_factory=list)
    trace_ids: list[str] = Field(default_factory=list)


class QueryMetricsInput(ToolInput):
    """One current runtime snapshot from a fixed Fault Lab service."""

    service: str = Field(min_length=1, max_length=100)
    environment: str = Field(min_length=1, max_length=50)


class MetricSnapshot(BaseModel):
    """The fields actually exposed by the Fault Lab metric snapshot."""

    model_config = ConfigDict(extra="forbid")

    service: str = Field(min_length=1)
    environment: str = Field(min_length=1)
    health_status: str = Field(min_length=1)
    request_count: int = Field(ge=0)
    success_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    error_rate: float = Field(ge=0, le=1)
    last_request_duration_ms: float | None = Field(default=None, ge=0)
    average_request_duration_ms: float | None = Field(default=None, ge=0)


class QueryMetricsOutput(ToolOutput):
    """Structured outcome for a single current Fault Lab metric snapshot."""

    metrics: MetricSnapshot | None = None

    @model_validator(mode="after")
    def validate_metrics_outcome(self) -> "QueryMetricsOutput":
        """Require a snapshot only when the whitelisted query succeeded."""
        if self.status is ToolStatus.SUCCESS and self.metrics is None:
            raise ValueError("successful metrics output must include metrics")
        if self.status is not ToolStatus.SUCCESS and self.metrics is not None:
            raise ValueError("unsuccessful metrics output must not include metrics")
        return self


class QueryTracesInput(TimeRangeToolInput):
    """Bounded distributed-trace query anchored to one Fault Lab service."""

    trace_id: str | None = Field(default=None, min_length=1, max_length=128)
    limit: int = Field(default=20, ge=1, le=100)


class TraceSpan(BaseModel):
    """A concise fact projection of one real ended OpenTelemetry span."""

    model_config = ConfigDict(extra="forbid")

    span_id: str = Field(min_length=1)
    parent_span_id: str | None = None
    service: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    start_time: datetime
    end_time: datetime
    duration_ms: float = Field(ge=0)
    status: str = Field(min_length=1)
    error: str | None = None


class TraceError(BaseModel):
    """A compact failure fact attached to a span in one trace."""

    model_config = ConfigDict(extra="forbid")

    service: str = Field(min_length=1)
    span_id: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    message: str = Field(min_length=1)


class TraceSummary(BaseModel):
    """One reconstructed trace with bounded, ordered service span facts."""

    model_config = ConfigDict(extra="forbid")

    trace_id: str = Field(min_length=1)
    duration_ms: float = Field(ge=0)
    status: str = Field(min_length=1)
    spans: list[TraceSpan] = Field(default_factory=list)
    errors: list[TraceError] = Field(default_factory=list)
    slowest_span: TraceSpan | None = None


class QueryTracesOutput(ToolOutput):
    """Structured outcome for reconstructing bounded Fault Lab traces."""

    traces: list[TraceSummary] = Field(default_factory=list)


class GetDeploymentHistoryInput(ToolInput):
    """Read the current and previous deployment facts for one Fault Lab service."""

    service: str = Field(min_length=1, max_length=100)
    environment: str = Field(min_length=1, max_length=50)


class DeploymentRecord(BaseModel):
    """The current and immediately previous deployment facts exposed by Fault Lab."""

    model_config = ConfigDict(extra="forbid")

    service: str = Field(min_length=1)
    environment: str = Field(min_length=1)
    current_version: str = Field(min_length=1)
    previous_version: str | None = None
    deployed_at: datetime | None = None


class GetDeploymentHistoryOutput(ToolOutput):
    """A single real deployment snapshot; Fault Lab has no longer history to return."""

    deployments: list[DeploymentRecord] = Field(default_factory=list)


class RollbackDeploymentInput(ToolInput):
    """Controlled rollback request shape; this task provides no executor for it."""

    service: str = Field(min_length=1, max_length=100)
    environment: str = Field(min_length=1, max_length=50)
    target_version: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2_000)
    approval_id: UUID


class RollbackDeploymentOutput(ToolOutput):
    """Future controlled rollback outcome; no rollback is executed in Task 2.6."""

    service: str = Field(min_length=1)
    environment: str = Field(min_length=1)
    target_version: str = Field(min_length=1)
    executed: bool = False
