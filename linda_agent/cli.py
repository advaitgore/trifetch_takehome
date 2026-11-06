"""CLI entry point for the Linda Fax Routing Agent.

Usage:
    python -m linda_agent.cli run [--headed] [--reset-checkpoint]
    python run_agent.py [--headed] [--reset-checkpoint]      (same thing)
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from linda_agent.agent import LindaAgent


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="TriFetch Linda Mini Fax Routing Agent")
    parser.add_argument(
        "command",
        nargs="?",
        choices=["run"],
        default="run",
        help="Command to execute (default: run)",
    )
    parser.add_argument("--headed", action="store_true", help="Run browser in visible (headed) mode")
    parser.add_argument("--reset-checkpoint", action="store_true", help="Clear checkpoints and start fresh")
    args = parser.parse_args(argv)

    if args.reset_checkpoint:
        from linda_agent.config import CHECKPOINT_FILE
        if CHECKPOINT_FILE.exists():
            CHECKPOINT_FILE.unlink()
            print(f"Cleared checkpoint: {CHECKPOINT_FILE}")

    agent = LindaAgent(headless=not args.headed)
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        print("\nAgent interrupted by user.")
        sys.exit(130)


if __name__ == "__main__":
    main()
