"""OpenSearch-backed normalized logs adapter for the OpenTelemetry Demo."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from devsupport_backend.config import Settings, settings
from devsupport_backend.tools.adapter_contracts import AdapterError, LogEvent, LogQueryResult
from devsupport_backend.tools.schemas import QueryLogsInput

INDEX_PATTERN = "otel-logs-*"
MAX_LOG_MESSAGE_CHARS = 2_000
SUPPORTED_ENVIRONMENT = "local"


class OpenSearchLogsAdapterError(AdapterError):
    """Safe, structured error returned by the OpenSearch logs adapter."""


class _OpenSearchResource(BaseModel):
    """The OpenTelemetry resource projection required for a log event."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    service: str = Field(alias="service.name", min_length=1)


class _OpenSearchSeverity(BaseModel):
    """The optional OpenTelemetry severity projection."""

    model_config = ConfigDict(extra="ignore")

    text: str = Field(min_length=1)


class _OpenSearchLogSource(BaseModel):
    """Only the bounded provider fields used by the normalized log contract."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    timestamp: datetime = Field(alias="@timestamp")
    body: str | int | float | bool
    resource: _OpenSearchResource
    severity: _OpenSearchSeverity | None = None
    trace_id: str | None = Field(default=None, alias="traceId")
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("body")
    @classmethod
    def validate_scalar_body(cls, value: object) -> str | int | float | bool:
        """Reject non-scalar provider bodies rather than serializing arbitrary payloads."""
        if isinstance(value, (str, int, float, bool)):
            return value
        raise ValueError("body must be a scalar")


class _OpenSearchHit(BaseModel):
    """One validated OpenSearch hit without exposing hit metadata."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    source: _OpenSearchLogSource = Field(alias="_source")


class _OpenSearchTotal(BaseModel):
    """The OpenSearch total-hits object returned with track_total_hits enabled."""

    model_config = ConfigDict(extra="ignore")

    value: int = Field(ge=0)
    relation: str | None = None


class _OpenSearchHits(BaseModel):
    """The subset of the OpenSearch hits response needed by this adapter."""

    model_config = ConfigDict(extra="ignore")

    total: _OpenSearchTotal | int
    hits: list[_OpenSearchHit]


class _OpenSearchSearchResponse(BaseModel):
    """A deliberately narrow, validated OpenSearch search response."""

    model_config = ConfigDict(extra="ignore")

    hits: _OpenSearchHits


class OpenSearchLogsAdapter:
    """Query bounded OpenTelemetry Demo logs through OpenSearch JSON DSL."""

    def __init__(
        self,
        *,
        opensearch_url: str,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._opensearch_url = opensearch_url.rstrip("/")
        self._http_client = http_client or httpx.Client(timeout=5.0)

    @classmethod
    def from_settings(cls, config: Settings = settings) -> "OpenSearchLogsAdapter":
        """Create the real logs provider only when its runtime endpoint is configured."""
        if not config.opensearch_url:
            raise OpenSearchLogsAdapterError(
                "missing_opensearch_configuration",
                "OpenSearch logs provider requires OPENSEARCH_URL.",
            )
        return cls(opensearch_url=config.opensearch_url)

    def query(self, tool_input: QueryLogsInput) -> LogQueryResult:
        """Return normalized events for one service and inclusive investigation window."""
        if tool_input.environment != SUPPORTED_ENVIRONMENT:
            raise OpenSearchLogsAdapterError(
                "unsupported_environment",
                "OpenSearch demo logs are only available for environment: local.",
            )

        try:
            response = self._http_client.post(
                f"{self._opensearch_url}/{INDEX_PATTERN}/_search",
                json=_search_body(tool_input),
            )
            response.raise_for_status()
            payload = _OpenSearchSearchResponse.model_validate(response.json())
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 429 or error.response.status_code >= 500:
                raise OpenSearchLogsAdapterError(
                    "opensearch_unavailable",
                    "OpenSearch logs provider is unavailable.",
                    retryable=True,
                ) from error
            raise OpenSearchLogsAdapterError(
                "opensearch_query_error",
                "OpenSearch rejected the logs query.",
            ) from error
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise OpenSearchLogsAdapterError(
                "opensearch_unavailable",
                "OpenSearch logs provider is unavailable.",
                retryable=True,
            ) from error
        except (ValidationError, ValueError) as error:
            raise OpenSearchLogsAdapterError(
                "invalid_opensearch_response",
                "OpenSearch returned an invalid logs response.",
            ) from error

        events = tuple(_to_log_event(hit.source, tool_input.service) for hit in payload.hits.hits)
        return LogQueryResult(
            match_count=_total_match_count(payload.hits.total),
            events=tuple(sorted(events, key=lambda event: event.timestamp)),
        )


def _search_body(tool_input: QueryLogsInput) -> dict[str, object]:
    """Build a fixed JSON DSL query with all user values kept as data values."""
    filters: list[dict[str, object]] = [
        {"term": {"resource.service.name": tool_input.service}},
        {
            "range": {
                "@timestamp": {
                    "gte": tool_input.time_range_start.isoformat(),
                    "lte": tool_input.time_range_end.isoformat(),
                }
            }
        },
    ]
    if tool_input.level:
        filters.append({"terms": {"severity.text.keyword": _severity_variants(tool_input.level)}})
    if tool_input.query:
        filters.append({"match": {"body": {"query": tool_input.query}}})
    return {
        "size": tool_input.limit,
        "track_total_hits": True,
        "_source": [
            "@timestamp",
            "body",
            "severity.text",
            "resource.service.name",
            "traceId",
            "attributes.exception.type",
        ],
        "query": {"bool": {"filter": filters}},
        "sort": [{"@timestamp": {"order": "desc"}}],
    }


def _severity_variants(level: str) -> list[str]:
    """Match the observed uppercase and lowercase OpenTelemetry severity values."""
    normalized = level.strip().lower()
    variants = {normalized, normalized.upper()}
    if normalized == "warn":
        variants.update({"warning", "WARNING"})
    return sorted(variants)


def _to_log_event(source: _OpenSearchLogSource, requested_service: str) -> LogEvent:
    """Map one validated provider source without leaking provider-private fields."""
    if source.resource.service != requested_service:
        raise OpenSearchLogsAdapterError(
            "service_mismatch",
            "OpenSearch returned logs for a different service.",
        )
    message = _bounded_message(source.body)
    if not message:
        raise OpenSearchLogsAdapterError(
            "invalid_opensearch_response",
            "OpenSearch returned a log event without a message.",
        )
    return LogEvent(
        timestamp=source.timestamp,
        service=source.resource.service,
        level=_normalize_severity(source.severity.text if source.severity else None),
        message=message,
        trace_id=_short_optional_text(source.trace_id),
        error_type=_short_optional_text(source.attributes.get("exception.type")),
    )


def _total_match_count(total: _OpenSearchTotal | int) -> int:
    """Use the complete total reported by the bounded query, not sample count."""
    return total if isinstance(total, int) else total.value


def _bounded_message(value: str | int | float | bool) -> str:
    """Convert only scalar provider bodies and cap the normalized Tool message."""
    return str(value)[:MAX_LOG_MESSAGE_CHARS]


def _short_optional_text(value: object) -> str | None:
    """Expose only short scalar correlation values when the provider supplied them."""
    if not isinstance(value, (str, int, float, bool)):
        return None
    text = str(value)
    return text[:MAX_LOG_MESSAGE_CHARS] if text else None


def _normalize_severity(value: str | None) -> str:
    """Map observed provider severity text to the Tool's canonical lowercase values."""
    if value is None:
        return "unknown"
    normalized = value.strip().lower()
    if normalized in {"error", "fatal", "critical"}:
        return "error"
    if normalized in {"warn", "warning"}:
        return "warn"
    if normalized in {"info", "information"}:
        return "info"
    if normalized in {"debug", "trace"}:
        return normalized
    return "unknown"
