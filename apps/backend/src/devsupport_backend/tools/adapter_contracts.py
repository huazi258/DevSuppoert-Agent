"""Provider-neutral contracts for read-only investigation evidence adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from devsupport_backend.tools.schemas import (
    GetDeploymentHistoryInput,
    QueryLogsInput,
    QueryMetricsInput,
    QueryTracesInput,
)

MAX_NORMALIZED_LOG_EVENTS = 100
MAX_NORMALIZED_TRACE_SPANS = 100
MAX_NORMALIZED_TEXT_CHARS = 2_000


@dataclass(frozen=True)
class AdapterProvenance:
    """Safe provider identity and observation time retained by normalized results."""

    source: str
    observed_at: datetime


def unknown_adapter_provenance() -> AdapterProvenance:
    """Keep compatibility adapters explicit when they have not declared a provider source."""
    return AdapterProvenance(source="unknown", observed_at=datetime.now(UTC))


class AdapterError(RuntimeError):
    """Safe failure boundary shared by all runtime-evidence adapters."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class NormalizedAdapterError:
    """Provider-neutral failure information safe to expose through a Tool result."""

    code: str
    message: str
    retryable: bool


def normalize_adapter_error(error: AdapterError) -> NormalizedAdapterError:
    """Hide provider implementation details behind the finite Tool error taxonomy."""
    if error.code == "capability_unavailable":
        return NormalizedAdapterError(
            code="capability_unavailable",
            message="The requested investigation capability is not enabled.",
            retryable=False,
        )
    if error.code == "timeout":
        return NormalizedAdapterError(
            code="timeout",
            message="The runtime evidence provider timed out.",
            retryable=True,
        )
    if error.code.startswith("invalid_") and error.code.endswith("response"):
        return NormalizedAdapterError(
            code="invalid_provider_response",
            message="The runtime evidence provider returned an invalid response.",
            retryable=False,
        )
    if error.code in {
        "unsupported_service",
        "unsupported_environment",
        "service_mismatch",
        "service_metrics_not_found",
    } or error.code.endswith("query_error"):
        return NormalizedAdapterError(
            code="invalid_request",
            message="The runtime evidence request is not supported by this provider.",
            retryable=False,
        )
    return NormalizedAdapterError(
        code="provider_unavailable",
        message="The runtime evidence provider is unavailable.",
        retryable=error.retryable or error.code.endswith("unavailable"),
    )


@dataclass(frozen=True)
class LogEvent:
    """One normalized, bounded runtime log fact."""

    timestamp: datetime
    service: str
    level: str
    message: str
    request_id: str | None = None
    trace_id: str | None = None
    error_type: str | None = None
    status_code: int | None = None
    duration_ms: float | None = None
    downstream_service: str | None = None


@dataclass(frozen=True)
class LogQueryResult:
    """A bounded set of normalized log events."""

    match_count: int
    events: tuple[LogEvent, ...]
    provenance: AdapterProvenance = field(default_factory=unknown_adapter_provenance)


@dataclass(frozen=True)
class MetricsQueryResult:
    """One service health and request-count snapshot."""

    service: str
    health_status: str
    request_count: int
    success_count: int
    error_count: int
    last_request_duration_ms: float | None
    average_request_duration_ms: float | None
    provenance: AdapterProvenance = field(default_factory=unknown_adapter_provenance)

    @property
    def error_rate(self) -> float:
        """Calculate a rate from current count facts without inventing history."""
        if self.request_count == 0:
            return 0.0
        return self.error_count / self.request_count


@dataclass(frozen=True)
class TraceSpanRecord:
    """One normalized ended span used to reconstruct bounded traces."""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    service: str
    operation: str
    start_time: datetime
    end_time: datetime
    duration_ms: float
    status: str
    error: str | None = None


@dataclass(frozen=True)
class TraceQueryResult:
    """A bounded set of normalized span facts."""

    spans: tuple[TraceSpanRecord, ...]
    provenance: AdapterProvenance = field(default_factory=unknown_adapter_provenance)


@dataclass(frozen=True)
class DeploymentQueryResult:
    """Current and previous deployment facts for one service."""

    service: str
    current_version: str
    previous_version: str | None
    deployed_at: datetime | None
    provenance: AdapterProvenance = field(default_factory=unknown_adapter_provenance)


@runtime_checkable
class LogsAdapter(Protocol):
    """Capability for querying normalized logs from one investigation target."""

    def query(self, tool_input: QueryLogsInput) -> LogQueryResult: ...


@runtime_checkable
class MetricsAdapter(Protocol):
    """Capability for querying one normalized metrics snapshot."""

    def query(self, tool_input: QueryMetricsInput) -> MetricsQueryResult: ...


@runtime_checkable
class TracesAdapter(Protocol):
    """Capability for querying normalized trace span facts."""

    def query(self, tool_input: QueryTracesInput) -> TraceQueryResult: ...


@runtime_checkable
class DeploymentAdapter(Protocol):
    """Capability for querying current and previous deployment facts."""

    def query(self, tool_input: GetDeploymentHistoryInput) -> DeploymentQueryResult: ...
