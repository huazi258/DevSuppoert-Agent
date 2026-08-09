"""Local deployment facts for the order-service Fault Lab."""

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

from order_service.runtime_state import restore_required_runtime_configuration

SERVICE_NAME = "order-service"
HEALTHY_VERSION = "v1.0.0"
FAULTY_VERSION = "v1.1.0"
STATE_PATH = Path(__file__).resolve().parents[2] / ".deployment-state.json"
_state_lock = Lock()


@dataclass(frozen=True)
class DeploymentState:
    service: str
    current_version: str
    previous_version: str | None
    deployed_at: str | None


def _baseline() -> DeploymentState:
    return DeploymentState(
        service=SERVICE_NAME,
        current_version=HEALTHY_VERSION,
        previous_version=None,
        deployed_at=None,
    )


def _load_state_unlocked() -> DeploymentState:
    if not STATE_PATH.exists():
        return _baseline()

    data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return DeploymentState(
        service=data["service"],
        current_version=data["current_version"],
        previous_version=data["previous_version"],
        deployed_at=data["deployed_at"],
    )


def get_deployment_state() -> DeploymentState:
    """Read the deployment facts exposed to future investigation adapters."""
    with _state_lock:
        return _load_state_unlocked()


def deploy_faulty_version() -> None:
    """Record the local deployment transition that activates Scenario A semantics."""
    with _state_lock:
        current = _load_state_unlocked()
        previous_version = current.previous_version
        if current.current_version != FAULTY_VERSION:
            previous_version = current.current_version
        deployment = DeploymentState(
            service=SERVICE_NAME,
            current_version=FAULTY_VERSION,
            previous_version=previous_version,
            deployed_at=datetime.now(UTC).isoformat(),
        )
        STATE_PATH.write_text(json.dumps(asdict(deployment)), encoding="utf-8")


class RollbackUnavailableError(ValueError):
    """The requested version is not the immediately previous deployment."""


@dataclass(frozen=True)
class RollbackResult:
    """Exact deployment state after a constrained rollback request."""

    deployment: DeploymentState
    executed: bool


def rollback_to_previous_version(target_version: str) -> RollbackResult:
    """Roll back only to the current deployment's recorded predecessor.

    The operation never deletes deployment, log, trace, or metrics history.
    """
    with _state_lock:
        current = _load_state_unlocked()
        if target_version == current.current_version:
            return RollbackResult(deployment=current, executed=False)
        if not current.previous_version or target_version != current.previous_version:
            raise RollbackUnavailableError(
                "target_version must equal the current deployment's previous_version"
            )
        deployment = DeploymentState(
            service=SERVICE_NAME,
            current_version=target_version,
            previous_version=current.current_version,
            deployed_at=datetime.now(UTC).isoformat(),
        )
        STATE_PATH.write_text(json.dumps(asdict(deployment)), encoding="utf-8")
    restore_required_runtime_configuration()
    return RollbackResult(deployment=deployment, executed=True)


def reset_deployment_state() -> None:
    """Restore the clean baseline without recording a synthetic deployment event."""
    with _state_lock:
        STATE_PATH.unlink(missing_ok=True)
