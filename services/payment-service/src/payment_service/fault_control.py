"""Local-only controls for reproducible payment-service Fault Lab scenarios."""

import argparse

import httpx

from payment_service.runtime_state import inject_payment_timeout

RESET_ENDPOINTS = (
    ("order-service", "http://127.0.0.1:8000/internal/fault-lab/reset"),
    ("payment-service", "http://127.0.0.1:8001/internal/fault-lab/reset"),
)
RESET_TIMEOUT_SECONDS = 5.0


class FaultLabResetError(RuntimeError):
    """Raised when the complete local Fault Lab reset cannot be verified."""


def reset_fault_lab() -> None:
    """Reset both live Fault Lab processes, including in-memory observability state."""
    for service, endpoint in RESET_ENDPOINTS:
        try:
            response = httpx.post(endpoint, timeout=RESET_TIMEOUT_SECONDS)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, RuntimeError, TypeError, ValueError) as error:
            raise FaultLabResetError(f"{service} reset failed: {error}") from error
        if payload != {"service": service, "status": "reset"}:
            raise FaultLabResetError(f"{service} reset returned an invalid response")


def main() -> None:
    """Run a local payment-service Fault Lab control command."""
    parser = argparse.ArgumentParser(description="Payment service Fault Lab control")
    commands = parser.add_subparsers(dest="command", required=True)
    inject_parser = commands.add_parser("inject")
    inject_parser.add_argument("fault", choices=["payment_timeout"])
    inject_parser.add_argument("--delay-seconds", type=float, default=None)
    commands.add_parser("reset")
    args = parser.parse_args()

    if args.command == "inject":
        if args.delay_seconds is None:
            inject_payment_timeout()
        else:
            inject_payment_timeout(args.delay_seconds)
        print("Injected payment_timeout")
        return

    try:
        reset_fault_lab()
    except FaultLabResetError as error:
        parser.error(str(error))
    print("Fault Lab reset")


if __name__ == "__main__":
    main()
