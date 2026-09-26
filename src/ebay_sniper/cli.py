"""Command line entry point.

Only the interface exists for now; the commands are implemented in milestone M1
(see CLAUDE.md).
"""

from __future__ import annotations

import argparse
import sys

from ebay_sniper import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ebay-sniper",
        description="Watch eBay for a specific item and notify via Telegram.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run-once", help="Run a single poll cycle and exit.")
    subparsers.add_parser("watch", help="Poll forever at the configured interval.")
    subparsers.add_parser(
        "check-config", help="Validate the configuration and print the daily API call budget."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(f"'{args.command}' is not implemented yet (milestone M1).", file=sys.stderr)
    return 1
