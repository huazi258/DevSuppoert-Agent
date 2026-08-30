"""Prometheus-backed normalized metrics adapter for the OpenTelemetry Demo."""

from __future__ import annotations

import math
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from devsupport_backend.config import Settings, settings
from devsupport_backend.tools.adapter_contracts import AdapterError, MetricsQueryResult
from devsupport_backend.tools.schemas import QueryMetricsInput

CALLS_METRIC = "traces_span_metrics_calls_total"
DURATION_COUNT_METRIC = "traces_span_metrics_duration_milliseconds_count"
DURATION_SUM_METRIC = "traces_span_metrics_duration_milliseconds_sum"
PRESENCE_METRIC = "target_info"
SERVER_SPAN_KIND = "SPAN_KIND_SERVER"
ERROR_STATUS_CODE = "STATUS_CODE_ERROR"
SERVICE_LABEL = "service_name"
SPAN_KIND_LABEL = "span_kind"
STATUS_CODE_LABEL = "status_code"
SUPPORTED_ENVIRONMENT = "local"


class PrometheusMetricsAdapterError(AdapterError):
    """Safe, structured error returned by the Prometheus metrics adapter."""


class _PrometheusVectorItem(BaseModel):
    """One Prometheus instant-vector item without retaining provider extras."""

    model_config = ConfigDict(extra="ignore")

    metric: dict[str, str]
    value: list[Any]


class _PrometheusQueryData(BaseModel):
    """Validated result envelope for one successful instant query."""

    model_config = ConfigDict(extra="ignore")

    result_type: str = Field(alias="resultType")
    result: list[_PrometheusVectorItem]


class _PrometheusQueryResponse(BaseModel):
    """Small Prometheus API response projection used by this provider."""

    model_config = ConfigDict(extra="ignore")

    status: str
    data: _PrometheusQueryData | None = None


class PrometheusMetricsAdapter:
    """Query cumulative OpenTelemetry Demo server span metrics through Prometheus."""

    def __init__(
        self,
        *,
        prometheus_url: str,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._prometheus_url = prometheus_url.rstrip("/")
        self._http_client = http_client or httpx.Client(timeout=5.0)

    @classmethod
    def from_settings(cls, config: Settings = settings) -> "PrometheusMetricsAdapter":
        """Create the real metrics provider only when its endpoint is configured."""
        if not config.prometheus_url:
            raise PrometheusMetricsAdapterError(
                "missing_prometheus_configuration",
                "Prometheus metrics provider requires PROMETHEUS_URL.",
            )
        return cls(prometheus_url=config.prometheus_url)

    def query(self, tool_input: QueryMetricsInput) -> MetricsQueryResult:
        """Return one cumulative server-span snapshot without inferring health facts."""
        if tool_input.environment != SUPPORTED_ENVIRONMENT:
            raise PrometheusMetricsAdapterError(
                "unsupported_environment",
                "Prometheus demo metrics are only available for environment: local.",
            )

        service = tool_input.service
        presence = self._query_value(_presence_query(service), service)
        if presence is None or presence <= 0:
            raise PrometheusMetricsAdapterError(
                "service_metrics_not_found",
                "Prometheus has no telemetry presence for the requested service.",
            )

        request_count = _as_count(
            self._query_value(_calls_query(service), service) or 0.0,
            "request count",
        )
        error_count = _as_count(
            self._query_value(_error_calls_query(service), service) or 0.0,
            "error count",
        )
        duration_count = _as_count(
            self._query_value(_duration_count_query(service), service) or 0.0,
            "duration count",
        )
        duration_sum = self._query_value(_duration_sum_query(service), service)
        if error_count > request_count:
            raise PrometheusMetricsAdapterError(
                "invalid_prometheus_response",
                "Prometheus returned an invalid error-count snapshot.",
            )
        average_duration_ms = _average_duration(duration_sum, duration_count)
        return MetricsQueryResult(
            service=service,
            health_status="unknown",
            request_count=request_count,
            success_count=request_count - error_count,
            error_count=error_count,
            last_request_duration_ms=None,
            average_request_duration_ms=average_duration_ms,
        )

    def _query_value(self, promql: str, requested_service: str) -> float | None:
        """Run one aggregate instant query and validate its single service result."""
        try:
            response = self._http_client.get(
                f"{self._prometheus_url}/api/v1/query",
                params={"query": promql},
            )
            response.raise_for_status()
            payload = _PrometheusQueryResponse.model_validate(response.json())
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 429 or error.response.status_code >= 500:
                raise PrometheusMetricsAdapterError(
                    "prometheus_unavailable",
                    "Prometheus metrics provider is unavailable.",
                    retryable=True,
                ) from error
            raise PrometheusMetricsAdapterError(
                "prometheus_query_error",
                "Prometheus rejected the metrics query.",
            ) from error
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise PrometheusMetricsAdapterError(
                "prometheus_unavailable",
                "Prometheus metrics provider is unavailable.",
                retryable=True,
            ) from error
        except (ValidationError, ValueError) as error:
            raise PrometheusMetricsAdapterError(
                "invalid_prometheus_response",
                "Prometheus returned an invalid metrics response.",
            ) from error

        if payload.status != "success":
            raise PrometheusMetricsAdapterError(
                "prometheus_query_error",
                "Prometheus rejected the metrics query.",
            )
        if payload.data is None or payload.data.result_type != "vector":
            raise PrometheusMetricsAdapterError(
                "invalid_prometheus_response",
                "Prometheus returned an invalid metrics response.",
            )
        if not payload.data.result:
            return None
        if len(payload.data.result) != 1:
            raise PrometheusMetricsAdapterError(
                "invalid_prometheus_response",
                "Prometheus returned multiple metric snapshots for one service.",
            )
        item = payload.data.result[0]
        if item.metric.get(SERVICE_LABEL) != requested_service:
            raise PrometheusMetricsAdapterError(
                "service_mismatch",
                "Prometheus returned metrics for a different service.",
            )
        return _parse_non_negative_number(item.value)


def _presence_query(service: str) -> str:
    """Return a resource-presence query without treating presence as health."""
    return f"count by ({SERVICE_LABEL}) ({PRESENCE_METRIC}{_service_matcher(service)})"


def _calls_query(service: str) -> str:
    """Return the cumulative inbound server-span call count query."""
    return _aggregate_query(CALLS_METRIC, service)


def _error_calls_query(service: str) -> str:
    """Return the cumulative inbound server-span error count query."""
    return _aggregate_query(
        CALLS_METRIC,
        service,
        additional_matchers={STATUS_CODE_LABEL: ERROR_STATUS_CODE},
    )


def _duration_count_query(service: str) -> str:
    """Return the cumulative server-span duration histogram count query."""
    return _aggregate_query(DURATION_COUNT_METRIC, service)


def _duration_sum_query(service: str) -> str:
    """Return the cumulative server-span duration histogram sum query in milliseconds."""
    return _aggregate_query(DURATION_SUM_METRIC, service)


def _aggregate_query(
    metric_name: str,
    service: str,
    *,
    additional_matchers: dict[str, str] | None = None,
) -> str:
    """Build one fixed server-span aggregate with safely quoted label values."""
    matchers = {
        SERVICE_LABEL: service,
        SPAN_KIND_LABEL: SERVER_SPAN_KIND,
        **(additional_matchers or {}),
    }
    rendered = ",".join(
        f'{label}="{_promql_string_literal(value)}"' for label, value in matchers.items()
    )
    return f"sum by ({SERVICE_LABEL}) ({metric_name}{{{rendered}}})"


def _service_matcher(service: str) -> str:
    """Render the only matcher used by service-presence lookup."""
    return f'{{{SERVICE_LABEL}="{_promql_string_literal(service)}"}}'


def _promql_string_literal(value: str) -> str:
    """Escape user-controlled label text without implementing a PromQL parser."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def _parse_non_negative_number(value: list[Any]) -> float:
    """Parse one Prometheus vector value while rejecting non-finite provider values."""
    if len(value) != 2:
        raise PrometheusMetricsAdapterError(
            "invalid_prometheus_response",
            "Prometheus returned an invalid metric value.",
        )
    try:
        numeric = float(value[1])
    except (TypeError, ValueError) as error:
        raise PrometheusMetricsAdapterError(
            "invalid_prometheus_response",
            "Prometheus returned an invalid metric value.",
        ) from error
    if not math.isfinite(numeric) or numeric < 0:
        raise PrometheusMetricsAdapterError(
            "invalid_prometheus_response",
            "Prometheus returned an invalid metric value.",
        )
    return numeric


def _as_count(value: float, name: str) -> int:
    """Require cumulative request counters to be non-negative whole numbers."""
    if not value.is_integer():
        raise PrometheusMetricsAdapterError(
            "invalid_prometheus_response",
            f"Prometheus returned an invalid {name}.",
        )
    return int(value)


def _average_duration(duration_sum: float | None, duration_count: int) -> float | None:
    """Calculate an average only from a valid cumulative millisecond histogram."""
    if duration_count == 0:
        return None
    if duration_sum is None or duration_sum < 0 or not math.isfinite(duration_sum):
        raise PrometheusMetricsAdapterError(
            "invalid_prometheus_response",
            "Prometheus returned an invalid duration histogram.",
        )
    return duration_sum / duration_count
