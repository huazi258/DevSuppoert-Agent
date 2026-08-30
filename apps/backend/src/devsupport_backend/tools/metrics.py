"""Whitelist-bound adapter for Fault Lab runtime metric snapshots."""

from __future__ import annotations

from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from devsupport_backend.config import Settings, settings
from devsupport_backend.tools.adapter_contracts import AdapterError, MetricsQueryResult
from devsupport_backend.tools.schemas import QueryMetricsInput

SUPPORTED_ENVIRONMENT = "local"
ServiceName = Literal["order-service", "payment-service"]


class MetricsAdapterError(AdapterError):
    """A safe, structured failure returned by the Fault Lab metrics adapter."""


class FaultLabMetricsResponse(BaseModel):
    """Validated current metric snapshot returned by a Fault Lab service."""

    model_config = ConfigDict(extra="forbid")

    service: ServiceName
    request_count: int = Field(ge=0)
    success_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    last_request_duration_ms: float | None = Field(default=None, ge=0)
    average_request_duration_ms: float | None = Field(default=None, ge=0)


class FaultLabHealthResponse(BaseModel):
    """Validated health response from the same whitelisted Fault Lab service."""

    model_config = ConfigDict(extra="forbid")

    service: ServiceName
    status: str = Field(min_length=1)


class FaultLabMetricsAdapter:
    """Read metrics and health only from the two fixed local Fault Lab endpoints."""

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
    def from_settings(cls, config: Settings = settings) -> "FaultLabMetricsAdapter":
        """Build the fixed Fault Lab service mapping from backend settings."""
        return cls(
            order_service_url=config.fault_lab_order_service_url,
            payment_service_url=config.fault_lab_payment_service_url,
        )

    def query(self, tool_input: QueryMetricsInput) -> MetricsQueryResult:
        """Fetch current metrics and health from exactly one permitted local service."""
        if tool_input.service not in self._service_urls:
            raise MetricsAdapterError(
                "unsupported_service",
                f"service is not available in the Fault Lab: {tool_input.service}",
            )
        if tool_input.environment != SUPPORTED_ENVIRONMENT:
            raise MetricsAdapterError(
                "unsupported_environment",
                f"Fault Lab metrics are only available for environment: {SUPPORTED_ENVIRONMENT}",
            )

        service_name: ServiceName = tool_input.service
        service_url = self._service_urls[service_name]
        try:
            metrics_response = self._http_client.get(f"{service_url}/internal/metrics")
            metrics_response.raise_for_status()
            health_response = self._http_client.get(f"{service_url}/health")
            health_response.raise_for_status()
            metrics = FaultLabMetricsResponse.model_validate(metrics_response.json())
            health = FaultLabHealthResponse.model_validate(health_response.json())
        except httpx.HTTPError as error:
            raise MetricsAdapterError(
                "fault_lab_unavailable",
                f"Fault Lab metrics endpoint request failed: {type(error).__name__}",
                retryable=True,
            ) from error
        except (ValidationError, ValueError) as error:
            raise MetricsAdapterError(
                "invalid_fault_lab_response",
                f"Fault Lab metrics endpoint returned invalid structured data: {error}",
            ) from error
        if metrics.service != service_name or health.service != service_name:
            raise MetricsAdapterError(
                "service_mismatch",
                f"Fault Lab endpoint did not return metrics and health for {service_name}",
            )
        return MetricsQueryResult(
            service=service_name,
            health_status=health.status,
            request_count=metrics.request_count,
            success_count=metrics.success_count,
            error_count=metrics.error_count,
            last_request_duration_ms=metrics.last_request_duration_ms,
            average_request_duration_ms=metrics.average_request_duration_ms,
        )
