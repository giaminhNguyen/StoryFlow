"""CLI: ``python -m storyflow.runtime --once | --run [--fake] [--database-url URL] ...``

Standard library only. The CLI never grants runners to sessions; discovery leaves runners
unassigned until the application service does it explicitly.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading

from ..logging_config import configure_logging
from .app import log_provider_readiness, SchemaError, build_runtime

logger = logging.getLogger("storyflow.runtime")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m storyflow.runtime", description="StoryFlow runtime loop")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="run a single iteration and exit")
    mode.add_argument("--run", action="store_true", help="loop until SIGINT/SIGTERM (or --max-iterations)")
    p.add_argument("--fake", action="store_true", help="fake subtitle client + deterministic fake runners")
    p.add_argument("--database-url", default=None)
    p.add_argument("--artifact-root", default=None)
    p.add_argument("--max-iterations", type=int, default=None)
    p.add_argument("--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="default: $STORYFLOW_LOG_LEVEL or INFO")
    p.add_argument("--log-dir", default=None, metavar="PATH",
                   help="also write a rotating storyflow.log into this directory (default: console only)")
    return p


def _log_iteration(report) -> None:
    logger.info("iteration %s", json.dumps(report.summary(), sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        handle = configure_logging(level=args.log_level, log_dir=args.log_dir)
    except (OSError, ValueError) as exc:
        print(f"storyflow.runtime: cannot set up logging: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    try:
        return _run(args)
    finally:
        handle.close()


def _run(args) -> int:
    try:
        app = build_runtime(database_url=args.database_url, artifact_root=args.artifact_root,
                            fake=args.fake, ensure_db_schema=True)
        log_provider_readiness(app)
    except SchemaError as exc:
        print(f"storyflow.runtime: startup failed: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary: turn any startup failure into an actionable exit code
        print(f"storyflow.runtime: startup failed: {type(exc).__name__}: {exc}. "
              "Check --database-url / --artifact-root and that the database is writable.", file=sys.stderr)
        return 2
    runtime = app.runtime
    try:
        if args.once:
            _log_iteration(runtime.run_once())
            return 0
        previous = {}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, lambda *_: runtime.request_stop())
        try:
            report = runtime.run_forever(max_iterations=args.max_iterations, on_iteration=_log_iteration)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        logger.info("stopped: %s after %d iterations", report.stop_reason, report.iterations)
        return 0
    finally:
        app.close()


if __name__ == "__main__":
    sys.exit(main())
