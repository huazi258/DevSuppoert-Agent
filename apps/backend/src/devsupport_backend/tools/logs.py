"""Whitelist-bound adapter for querying Fault Lab structured logs."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from devsupport_backend.config import Settings, settings
from devsupport_backend.tools.adapter_contracts import AdapterError, LogEvent, LogQueryResult
from devsupport_backend.tools.schemas import QueryLogsInput

SUPPORTED_ENVIRONMENT = "local"
ServiceName = Literal["order-service", "payment-service"]


class LogsAdapterError(AdapterError):
    """A safe, structured failure returned by the Fault Lab logs adapter."""


class FaultLabLogEvent(BaseModel):
    """Validated representation of a JsonFormatter event from the Fault Lab."""

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    service: ServiceName
    level: str = Field(min_length=1)
    message: str = Field(min_length=1)
    method: str | None = None
    path: str | None = None
    status_code: int | None = None
    duration_ms: float | None = Field(default=None, ge=0)
    request_id: str | None = None
    downstream_service: str | None = None
    downstream_status: int | None = None
    error_type: str | None = None
    version: str | None = None
    config_key: str | None = None
    trace_id: str | None = None
    span_id: str | None = None


class FaultLabLogsResponse(BaseModel):
    """Internal response contract exposed by a whitelisted Fault Lab service."""

    model_config = ConfigDict(extra="forbid")

    service: ServiceName
    match_count: int = Field(ge=0)
    events: list[FaultLabLogEvent]


class FaultLabLogsAdapter:
    """Read logs only from the two fixed local Fault Lab service endpoints."""

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
    def from_settings(cls, config: Settings = settings) -> "FaultLabLogsAdapter":
        """Create the only supported service-to-endpoint mapping from backend settings."""
        return cls(
            order_service_url=config.fault_lab_order_service_url,
            payment_service_url=config.fault_lab_payment_service_url,
        )

    def query(self, tool_input: QueryLogsInput) -> LogQueryResult:
        """Fetch a bounded, server-filtered log window from a whitelisted service."""
        if tool_input.service not in self._service_urls:
            raise LogsAdapterError(
                "unsupported_service",
                f"service is not available in the Fault Lab: {tool_input.service}",
            )
        if tool_input.environment != SUPPORTED_ENVIRONMENT:
            raise LogsAdapterError(
                "unsupported_environment",
                f"Fault Lab logs are only available for environment: {SUPPORTED_ENVIRONMENT}",
            )

        service_name: ServiceName = tool_input.service
        try:
            response = self._http_client.get(
                f"{self._service_urls[service_name]}/internal/logs",
                params={
                    "time_range_start": tool_input.time_range_start.isoformat(),
                    "time_range_end": tool_input.time_range_end.isoformat(),
                    "level": tool_input.level,
                    "query": tool_input.query,
                    "limit": tool_input.limit,
                },
            )
            response.raise_for_status()
            payload = FaultLabLogsResponse.model_validate(response.json())
        except httpx.HTTPError as error:
            raise LogsAdapterError(
                "fault_lab_unavailable",
                f"Fault Lab log endpoint request failed: {type(error).__name__}",
                retryable=True,
            ) from error
        except (ValidationError, ValueError) as error:
            raise LogsAdapterError(
                "invalid_fault_lab_response",
                f"Fault Lab log endpoint returned invalid structured data: {error}",
            ) from error
        if payload.service != service_name:
            raise LogsAdapterError(
                "service_mismatch",
                f"Fault Lab endpoint returned logs for {payload.service}, expected {service_name}",
            )
        return LogQueryResult(
            match_count=payload.match_count,
            events=tuple(
                LogEvent(
                    timestamp=event.timestamp,
                    service=event.service,
                    level=event.level,
                    message=event.message,
                    request_id=event.request_id,
                    trace_id=event.trace_id,
                    error_type=event.error_type,
                    status_code=event.status_code,
                    duration_ms=event.duration_ms,
                    downstream_service=event.downstream_service,
                )
                for event in payload.events
            ),
        )
