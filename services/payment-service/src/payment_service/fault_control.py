"""Local-only controls for reproducible payment-service Fault Lab scenarios."""

import argparse

from payment_service.runtime_state import inject_payment_timeout, reset_runtime_state


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

    reset_runtime_state()
    print("Fault Lab reset")


if __name__ == "__main__":
    main()
