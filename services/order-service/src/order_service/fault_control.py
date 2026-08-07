"""Local-only controls for reproducible Fault Lab scenarios."""

import argparse

from order_service.runtime_state import inject_missing_config, reset_runtime_state


def main() -> None:
    """Run a local Fault Lab control command."""
    parser = argparse.ArgumentParser(description="Order service Fault Lab control")
    commands = parser.add_subparsers(dest="command", required=True)
    inject_parser = commands.add_parser("inject")
    inject_parser.add_argument("fault", choices=["missing_config"])
    commands.add_parser("reset")
    args = parser.parse_args()

    if args.command == "inject":
        inject_missing_config()
        print("Injected missing_config")
        return

    reset_runtime_state()
    print("Fault Lab reset")


if __name__ == "__main__":
    main()
