"""Local-only controls for reproducible Fault Lab scenarios."""

import argparse

from order_service.deployment_state import deploy_faulty_version, reset_deployment_state
from order_service.runtime_state import inject_missing_config, reset_runtime_state


def inject_missing_config_fault() -> None:
    """Activate Scenario A's deployment and runtime configuration conditions."""
    deploy_faulty_version()
    inject_missing_config()


def reset_fault_lab() -> None:
    """Restore deployment and runtime state to the Fault Lab baseline."""
    reset_deployment_state()
    reset_runtime_state()


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

    reset_fault_lab()
    print("Fault Lab reset")


if __name__ == "__main__":
    main()
