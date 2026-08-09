"""Local runtime state for reproducible order-service fault-lab scenarios."""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Lock

STATE_PATH = Path(__file__).resolve().parents[2] / ".fault-lab-state.json"
_state_lock = Lock()


@dataclass
class RuntimeMetrics:
    request_count: int = 0
    success_count: int = 0
    error_count: int = 0
    last_request_duration_ms: float | None = None


@dataclass
class RuntimeState:
    payment_timeout_configured: bool = True
    metrics: RuntimeMetrics = field(default_factory=RuntimeMetrics)


def _default_state() -> RuntimeState:
    return RuntimeState()


def _load_state_unlocked() -> RuntimeState:
    if not STATE_PATH.exists():
        return _default_state()

    data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    metrics = data.get("metrics", {})
    return RuntimeState(
        payment_timeout_configured=data["payment_timeout_configured"],
        metrics=RuntimeMetrics(**metrics),
    )


def _save_state_unlocked(state: RuntimeState) -> None:
    STATE_PATH.write_text(json.dumps(asdict(state)), encoding="utf-8")


def get_runtime_state() -> RuntimeState:
    """Read the current local fault-lab runtime state."""
    with _state_lock:
        return _load_state_unlocked()


def inject_missing_config() -> None:
    """Activate the runtime configuration condition used by Scenario A."""
    with _state_lock:
        _save_state_unlocked(
            RuntimeState(
                payment_timeout_configured=False,
            )
        )


def restore_required_runtime_configuration() -> None:
    """Restore only the runtime setting coupled to the previous deployment.

    This is deliberately not the Fault Lab reset: accumulated metrics remain
    available as investigation and recovery evidence.
    """
    with _state_lock:
        state = _load_state_unlocked()
        state.payment_timeout_configured = True
        _save_state_unlocked(state)


def reset_runtime_state() -> None:
    """Return the fault lab to the default healthy state and clear metrics."""
    with _state_lock:
        STATE_PATH.unlink(missing_ok=True)


def record_order_result(*, succeeded: bool, duration_ms: float) -> None:
    """Record lightweight runtime evidence for an order request."""
    with _state_lock:
        state = _load_state_unlocked()
        state.metrics.request_count += 1
        state.metrics.last_request_duration_ms = duration_ms
        if succeeded:
            state.metrics.success_count += 1
        else:
            state.metrics.error_count += 1
        _save_state_unlocked(state)


def metrics_snapshot() -> dict[str, int | float | None]:
    """Return runtime metrics without exposing the active fault or diagnosis."""
    metrics = get_runtime_state().metrics
    return asdict(metrics)
