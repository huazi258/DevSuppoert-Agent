"""Local runtime state for reproducible payment-service fault-lab scenarios."""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Lock

STATE_PATH = Path(__file__).resolve().parents[2] / ".fault-lab-state.json"
DEFAULT_DELAY_SECONDS = 4.0
_state_lock = Lock()


@dataclass
class RuntimeMetrics:
    request_count: int = 0
    success_count: int = 0
    error_count: int = 0
    last_request_duration_ms: float | None = None
    total_request_duration_ms: float = 0.0


@dataclass
class RuntimeState:
    response_delay_seconds: float = 0.0
    metrics: RuntimeMetrics = field(default_factory=RuntimeMetrics)


def _default_state() -> RuntimeState:
    return RuntimeState()


def _load_state_unlocked() -> RuntimeState:
    if not STATE_PATH.exists():
        return _default_state()

    data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return RuntimeState(
        response_delay_seconds=data["response_delay_seconds"],
        metrics=RuntimeMetrics(**data.get("metrics", {})),
    )


def _save_state_unlocked(state: RuntimeState) -> None:
    STATE_PATH.write_text(json.dumps(asdict(state)), encoding="utf-8")


def response_delay_seconds() -> float:
    """Return the currently configured downstream response delay."""
    with _state_lock:
        return _load_state_unlocked().response_delay_seconds


def inject_payment_timeout(delay_seconds: float = DEFAULT_DELAY_SECONDS) -> None:
    """Configure a real response delay that exceeds the caller timeout by default."""
    if delay_seconds <= 0:
        raise ValueError("delay_seconds must be greater than zero")

    with _state_lock:
        _save_state_unlocked(RuntimeState(response_delay_seconds=delay_seconds))


def reset_runtime_state() -> None:
    """Clear the payment fault and its runtime metrics."""
    with _state_lock:
        STATE_PATH.unlink(missing_ok=True)


def record_payment_result(*, succeeded: bool, duration_ms: float) -> None:
    """Record lightweight request evidence without publishing fault metadata."""
    with _state_lock:
        state = _load_state_unlocked()
        metrics = state.metrics
        metrics.request_count += 1
        metrics.last_request_duration_ms = duration_ms
        metrics.total_request_duration_ms += duration_ms
        if succeeded:
            metrics.success_count += 1
        else:
            metrics.error_count += 1
        _save_state_unlocked(state)


def metrics_snapshot() -> dict[str, int | float | None]:
    """Return only runtime metrics suitable for a future metrics adapter."""
    with _state_lock:
        metrics = _load_state_unlocked().metrics
        average_duration = None
        if metrics.request_count:
            average_duration = metrics.total_request_duration_ms / metrics.request_count
        return {
            "request_count": metrics.request_count,
            "success_count": metrics.success_count,
            "error_count": metrics.error_count,
            "last_request_duration_ms": metrics.last_request_duration_ms,
            "average_request_duration_ms": average_duration,
        }
