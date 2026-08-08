"""Whitelist-bound adapter for Fault Lab deployment facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from devsupport_backend.config import Settings, settings
from devsupport_backend.tools.schemas import GetDeploymentHistoryInput

SUPPORTED_ENVIRONMENT = "local"
ServiceName = Literal["order-service", "payment-service"]


class DeploymentAdapterError(RuntimeError):
    """A safe, structured failure returned by the Fault Lab deployment adapter."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class FaultLabDeploymentResponse(BaseModel):
    """Validated current and previous deployment facts from a Fault Lab service."""

    model_config = ConfigDict(extra="forbid")

    service: ServiceName
    current_version: str = Field(min_length=1, max_length=100)
    previous_version: str | None = Field(default=None, max_length=100)
    deployed_at: datetime | None = None


@dataclass(frozen=True)
class DeploymentQueryResult:
    """One service-local deployment snapshot, without manufactured history."""

    service: ServiceName
    current_version: str
    previous_version: str | None
    deployed_at: datetime | None


class FaultLabDeploymentAdapter:
    """Read deployment facts only from the two fixed local Fault Lab endpoints."""

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
    def from_settings(cls, config: Settings = settings) -> "FaultLabDeploymentAdapter":
        """Build the fixed Fault Lab service mapping from backend settings."""
        return cls(
            order_service_url=config.fault_lab_order_service_url,
            payment_service_url=config.fault_lab_payment_service_url,
        )

    def query(self, tool_input: GetDeploymentHistoryInput) -> DeploymentQueryResult:
        """Fetch only the selected service's fixed deployment endpoint."""
        if tool_input.service not in self._service_urls:
            raise DeploymentAdapterError(
                "unsupported_service",
                f"service is not available in the Fault Lab: {tool_input.service}",
            )
        if tool_input.environment != SUPPORTED_ENVIRONMENT:
            raise DeploymentAdapterError(
                "unsupported_environment",
                "Fault Lab deployments are only available for environment: "
                f"{SUPPORTED_ENVIRONMENT}",
            )

        service_name: ServiceName = tool_input.service
        try:
            endpoint = f"{self._service_urls[service_name]}/internal/deployment"
            response = self._http_client.get(endpoint)
            response.raise_for_status()
            deployment = FaultLabDeploymentResponse.model_validate(response.json())
        except httpx.HTTPError as error:
            raise DeploymentAdapterError(
                "fault_lab_unavailable",
                f"Fault Lab deployment endpoint request failed: {type(error).__name__}",
                retryable=True,
            ) from error
        except (ValidationError, ValueError) as error:
            raise DeploymentAdapterError(
                "invalid_fault_lab_response",
                f"Fault Lab deployment endpoint returned invalid structured data: {error}",
            ) from error
        if deployment.service != service_name:
            raise DeploymentAdapterError(
                "service_mismatch",
                "Fault Lab endpoint returned deployment facts for "
                f"{deployment.service}, expected {service_name}",
            )
        return DeploymentQueryResult(
            service=service_name,
            current_version=deployment.current_version,
            previous_version=deployment.previous_version,
            deployed_at=deployment.deployed_at,
        )
