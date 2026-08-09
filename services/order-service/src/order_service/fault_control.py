"""Local-only controls for reproducible Fault Lab scenarios."""

import argparse

import httpx

from order_service.deployment_state import deploy_faulty_version
from order_service.runtime_state import inject_missing_config

RESET_ENDPOINTS = (
    ("order-service", "http://127.0.0.1:8000/internal/fault-lab/reset"),
    ("payment-service", "http://127.0.0.1:8001/internal/fault-lab/reset"),
)
RESET_TIMEOUT_SECONDS = 5.0


class FaultLabResetError(RuntimeError):
    """Raised when the complete local Fault Lab reset cannot be verified."""


def inject_missing_config_fault() -> None:
    """Activate Scenario A's deployment and runtime configuration conditions."""
    deploy_faulty_version()
    inject_missing_config()


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
    """Run a local Fault Lab control command."""
    parser = argparse.ArgumentParser(description="Order service Fault Lab control")
    commands = parser.add_subparsers(dest="command", required=True)
    inject_parser = commands.add_parser("inject")
    inject_parser.add_argument("fault", choices=["missing_config"])
    commands.add_parser("reset")
    args = parser.parse_args()

    if args.command == "inject":
        inject_missing_config_fault()
        print("Injected missing_config")
        return

    try:
        reset_fault_lab()
    except FaultLabResetError as error:
        parser.error(str(error))
    print("Fault Lab reset")


if __name__ == "__main__":
    main()
