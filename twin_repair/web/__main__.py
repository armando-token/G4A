"""CLI entrypoint for the BlackboxDC Twin-Repair 4-State Web Application.

Usage:
    python -m twin_repair.web --host 127.0.0.1 --port 8000 --runs runs
"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from twin_repair.web.app import create_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BlackboxDC Twin-Repair Web Server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    parser.add_argument("--runs", default="runs", help="Directory containing run bundles")
    parser.add_argument(
        "--max-public-live-runs",
        type=int,
        default=2,
        help="Maximum public live runs before falling back to recorded showcase runs",
    )
    args = parser.parse_args(argv)

    app = create_app(
        runs_root=Path(args.runs),
        max_public_live_runs=args.max_public_live_runs,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
