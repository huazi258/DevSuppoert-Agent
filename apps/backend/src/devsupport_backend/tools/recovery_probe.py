"""Fixed recovery probe, deliberately outside the Agent Tool registry."""
# ruff: noqa: E501

from dataclasses import dataclass

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from devsupport_backend.config import Settings, settings


class RecoveryProbeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(min_length=1)
    payment_id: str = Field(min_length=1)
    status: str = Field(min_length=1)


@dataclass(frozen=True)
class RecoveryProbeResult:
    outcome: str
    http_status: int | None
    response_status: str | None


class FaultLabRecoveryProbeAdapter:
    """Probe only POST /orders on the settings-bound local order service."""

    def __init__(self, *, order_service_url: str, http_client: httpx.Client | None = None) -> None:
        self._order_service_url = order_service_url.rstrip("/")
        self._http_client = http_client or httpx.Client(timeout=5.0)

    @classmethod
    def from_settings(cls, config: Settings = settings) -> "FaultLabRecoveryProbeAdapter":
        return cls(order_service_url=config.fault_lab_order_service_url)

    def probe(self) -> RecoveryProbeResult:
        try:
            response = self._http_client.post(
                f"{self._order_service_url}/orders", json={"amount": 1.0}
            )
        except httpx.HTTPError:
            return RecoveryProbeResult("inconclusive", None, None)
        if response.status_code != 200:
            return RecoveryProbeResult("fail", response.status_code, None)
        try:
            payload = RecoveryProbeResponse.model_validate(response.json())
        except (ValidationError, ValueError):
            return RecoveryProbeResult("fail", response.status_code, None)
        if payload.status != "confirmed":
            return RecoveryProbeResult("fail", response.status_code, payload.status)
        return RecoveryProbeResult("pass", response.status_code, payload.status)
