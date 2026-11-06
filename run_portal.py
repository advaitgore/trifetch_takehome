#!/usr/bin/env python3
"""Convenience launcher for the Linda Mini Fax Portal with optional fault modes."""
import argparse
import os
import sys
import uvicorn

def main():
    parser = argparse.ArgumentParser(description="Start Linda Mini Fax Portal")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on (default: 8000)")
    parser.add_argument("--host", default="127.0.0.1", help="Host to listen on (default: 127.0.0.1)")
    parser.add_argument(
        "--fault",
        choices=["none", "download_503_once", "session_expire_on_route_once", "route_commit_then_503_once"],
        default="none",
        help="Simulated fault mode"
    )
    args = parser.parse_args()

    if args.fault != "none":
        os.environ["LINDA_FAULT_MODE"] = args.fault
        print(f"Starting portal with LINDA_FAULT_MODE={args.fault}")

    uvicorn.run("portal.app:app", host=args.host, port=args.port, log_level="info")

if __name__ == "__main__":
    main()
