"""Stable deployment facts for payment-service in the local Fault Lab."""

from dataclasses import dataclass

SERVICE_NAME = "payment-service"
CURRENT_VERSION = "v1.0.0"


@dataclass(frozen=True)
class DeploymentState:
    service: str = SERVICE_NAME
    current_version: str = CURRENT_VERSION
    previous_version: str | None = None
    deployed_at: str | None = None


def get_deployment_state() -> DeploymentState:
    """Return stable deployment facts unaffected by runtime timeout injection."""
    return DeploymentState()
