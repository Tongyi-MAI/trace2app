"""``t2w-serve``: host a Trace2World release (or one environment) over HTTP."""

from __future__ import annotations

import argparse
import logging

import uvicorn

from trace2world_env.catalog import load_catalog
from trace2world_env.server import create_app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse ``t2w-serve`` arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--environments-dir",
        help="Release directory with environments_manifest.json; tasks select the environment.",
    )
    source.add_argument(
        "--environment-dir",
        help="One environment directory (environments/<id>).",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind address.")
    parser.add_argument("--port", type=int, default=6800, help="Port to serve on.")
    parser.add_argument("--state-root", help="Writable directory for SQLite episode state.")
    parser.add_argument("--browser-executable", help="Chromium executable to launch.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Load the catalog and serve it until interrupted."""
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    # Every state-kernel call is an httpx request; keep them out of the host log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    catalog = load_catalog(args.environments_dir or args.environment_dir)
    app = create_app(
        catalog,
        port=args.port,
        state_root=args.state_root,
        browser_executable=args.browser_executable,
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
