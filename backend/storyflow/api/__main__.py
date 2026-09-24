"""CLI: ``python -m storyflow.api [--host 127.0.0.1] [--port 8765] [--fake] [--no-runtime] ...``

Loopback only by default. A non-loopback ``--host`` is refused (exit code 2) unless
``--allow-non-loopback`` is given, in which case a warning is logged: THE API HAS NO AUTHENTICATION.
Graceful shutdown is uvicorn's SIGINT/SIGTERM handling -> lifespan shutdown -> ``RuntimeHost.stop()``.
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import sys

from ..runtime.app import SchemaError, build_runtime
from .app import create_app

logger = logging.getLogger("storyflow.api")


def is_loopback_host(host: str) -> bool:
    h = host.strip().lower().strip("[]")
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m storyflow.api", description="StoryFlow local HTTP API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--fake", action="store_true", help="fake subtitle client + deterministic fake runners")
    p.add_argument("--database-url", default=None)
    p.add_argument("--artifact-root", default=None)
    p.add_argument("--no-runtime", action="store_true", help="API only; an external `storyflow.runtime --run` owns the loop")
    p.add_argument("--allow-non-loopback", action="store_true", help="permit a non-loopback --host (NO authentication!)")
    p.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    return p.parse_args(argv)


def build_server_app(args: argparse.Namespace):
    """Testable seam: returns (fastapi_app, runtime_app). Caller closes runtime_app."""
    runtime_app = build_runtime(database_url=args.database_url, artifact_root=args.artifact_root,
                                fake=args.fake, ensure_db_schema=True)
    extra_hosts = [] if is_loopback_host(args.host) else [args.host]
    return create_app(runtime_app, run_runtime=not args.no_runtime, allowed_hosts=extra_hosts), runtime_app


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not is_loopback_host(args.host):
        if not args.allow_non_loopback:
            print(f"storyflow.api: refusing to bind non-loopback host {args.host!r} (the API has no "
                  "authentication); pass --allow-non-loopback to override.", file=sys.stderr)
            return 2
        logger.warning("binding %s: the API has NO authentication; anyone who can reach it can control workflows",
                       args.host)
    try:
        api, runtime_app = build_server_app(args)
    except SchemaError as exc:
        print(f"storyflow.api: startup failed: {exc}", file=sys.stderr)
        return 2
    import uvicorn
    try:
        uvicorn.run(api, host=args.host, port=args.port, log_level=args.log_level)
    finally:
        runtime_app.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
