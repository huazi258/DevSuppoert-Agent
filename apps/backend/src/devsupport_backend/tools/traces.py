"""Whitelist-bound adapter for collecting Fault Lab OpenTelemetry span facts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from devsupport_backend.config import Settings, settings
from devsupport_backend.tools.adapter_contracts import (
    AdapterError,
    TraceQueryResult,
    TraceSpanRecord,
)
from devsupport_backend.tools.schemas import QueryTracesInput

SUPPORTED_ENVIRONMENT = "local"
ServiceName = Literal["order-service", "payment-service"]


class TracesAdapterError(AdapterError):
    """A safe, structured failure returned by the Fault Lab trace adapter."""


class FaultLabTraceSpan(BaseModel):
    """Validated projection of one real ended span from a Fault Lab service."""

    model_config = ConfigDict(extra="forbid")

    trace_id: str = Field(min_length=1, max_length=128)
    span_id: str = Field(min_length=1, max_length=128)
    parent_span_id: str | None = Field(default=None, max_length=128)
    service: ServiceName
    operation: str = Field(min_length=1, max_length=500)
    start_time: datetime
    end_time: datetime
    duration_ms: float = Field(ge=0)
    status: str = Field(min_length=1, max_length=50)
    error: str | None = Field(default=None, max_length=1_000)


class FaultLabTracesResponse(BaseModel):
    """Internal response contract exposed by a whitelisted Fault Lab service."""

    model_config = ConfigDict(extra="forbid")

    service: ServiceName
    match_count: int = Field(ge=0)
    spans: list[FaultLabTraceSpan]


class FaultLabTracesAdapter:
    """Read span facts only from the two fixed local Fault Lab trace endpoints."""

    def __init__(
        self,
        *,
        order_service_url: str,
        payment_service_url: str,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._service_urls: dict[ServiceName, str] = {
            "order-service": order_service_url.rstrip("/"),
            "payment-service": payment_service_url.rstrip("/"),
        }
        self._http_client = http_client or httpx.Client(timeout=5.0)

    @classmethod
    def from_settings(cls, config: Settings = settings) -> "FaultLabTracesAdapter":
        """Build the fixed Fault Lab service mapping from backend settings."""
        return cls(
            order_service_url=config.fault_lab_order_service_url,
            payment_service_url=config.fault_lab_payment_service_url,
        )

    def query(self, tool_input: QueryTracesInput) -> TraceQueryResult:
        """Fetch both service-local span buffers to reconstruct distributed traces."""
        if tool_input.service not in self._service_urls:
            raise TracesAdapterError(
                "unsupported_service",
                f"service is not available in the Fault Lab: {tool_input.service}",
            )
        if tool_input.environment != SUPPORTED_ENVIRONMENT:
            raise TracesAdapterError(
                "unsupported_environment",
                f"Fault Lab traces are only available for environment: {SUPPORTED_ENVIRONMENT}",
            )

        spans: list[FaultLabTraceSpan] = []
        for service_name, service_url in self._service_urls.items():
            params = {
                "time_range_start": tool_input.time_range_start.isoformat(),
                "time_range_end": tool_input.time_range_end.isoformat(),
                "limit": tool_input.limit,
            }
            if tool_input.trace_id is not None:
                params["trace_id"] = tool_input.trace_id
            try:
                response = self._http_client.get(
                    f"{service_url}/internal/traces",
                    params=params,
                )
                response.raise_for_status()
                payload = FaultLabTracesResponse.model_validate(response.json())
            except httpx.HTTPError as error:
                raise TracesAdapterError(
                    "fault_lab_unavailable",
                    f"Fault Lab trace endpoint request failed: {type(error).__name__}",
                    retryable=True,
                ) from error
            except (ValidationError, ValueError) as error:
                raise TracesAdapterError(
                    "invalid_fault_lab_response",
                    f"Fault Lab trace endpoint returned invalid structured data: {error}",
                ) from error
            if payload.service != service_name:
                raise TracesAdapterError(
                    "service_mismatch",
                    "Fault Lab endpoint returned traces for "
                    f"{payload.service}, expected {service_name}",
                )
            spans.extend(payload.spans)
        return TraceQueryResult(
            spans=tuple(
                TraceSpanRecord(
                    trace_id=span.trace_id,
                    span_id=span.span_id,
                    parent_span_id=span.parent_span_id,
                    service=span.service,
                    operation=span.operation,
                    start_time=span.start_time,
                    end_time=span.end_time,
                    duration_ms=span.duration_ms,
                    status=span.status,
                    error=span.error,
                )
                for span in spans
            )
        )
