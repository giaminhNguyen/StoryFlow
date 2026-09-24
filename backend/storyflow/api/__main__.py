"""CLI: ``python -m storyflow.api [--host 127.0.0.1] [--port 8765] [--fake] [--no-runtime] ...``

Loopback only by default. A non-loopback ``--host`` is refused (exit code 2) unless
``--allow-non-loopback`` is given, in which case a warning is logged: THE API HAS NO AUTHENTICATION.

Serving: the built frontend (``<repo>/frontend/dist`` by default, ``--frontend-dir`` to override,
``--no-frontend`` to disable) is served at ``/`` next to the API. ``--open-browser`` opens it once the
server accepts connections (best effort). Logging: console + rotating file ``runtime/logs/storyflow.log``
(``--log-dir`` to move it, ``--no-log-file`` for console only); see ``storyflow.logging_config``.

Startup failures (bad config, unusable database, port in use, unwritable log dir) print ONE actionable line
to stderr and exit with code 2 (no traceback).

Graceful shutdown: SIGINT / SIGTERM / (Windows) CTRL_BREAK -> uvicorn -> lifespan shutdown ->
``RuntimeHost.stop()`` (joins the runtime thread) -> ``RuntimeApp.close()`` (disposes the engine) ->
the final log line ``shutdown complete``. Exit code 0.
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import signal
import socket
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Callable

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError

from ..logging_config import LEVELS, configure_logging
from ..runtime.app import log_provider_readiness, SchemaError, build_runtime
from .app import create_app
from .routes import VERSION

logger = logging.getLogger("storyflow.api")

REPO_ROOT = Path(__file__).resolve().parents[3]
BROWSER_WAIT_SECONDS = 30.0


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
    p.add_argument("--log-level", default=None, choices=list(LEVELS),
                   help="default: $STORYFLOW_LOG_LEVEL or info")
    p.add_argument("--frontend-dir", default=None, metavar="PATH",
                   help="built frontend (contains index.html); default <repo>/frontend/dist")
    p.add_argument("--no-frontend", action="store_true", help="API only: do not serve the frontend")
    p.add_argument("--log-dir", default=None, metavar="PATH",
                   help="directory for storyflow.log (default $STORYFLOW_LOG_DIR or runtime/logs)")
    p.add_argument("--log-file", action=argparse.BooleanOptionalAction, default=True,
                   help="rotating log file (default on; --no-log-file = console only)")
    p.add_argument("--open-browser", action="store_true", help="open the UI in the default browser once listening")
    return p.parse_args(argv)


def resolve_frontend_dir(args: argparse.Namespace, repo_root: Path | None = None) -> Path | None:
    """None = do not serve. Otherwise the directory to serve (it may lack index.html: ``create_app`` then
    logs the build-command warning and the API keeps working)."""
    if args.no_frontend:
        return None
    if args.frontend_dir:
        return Path(args.frontend_dir)
    return (REPO_ROOT if repo_root is None else Path(repo_root)) / "frontend" / "dist"


def database_label(url: str | None) -> str:
    """Credential- and path-free description of the database for the banner."""
    if not url:
        return "default"
    try:
        u = make_url(url)
    except (ArgumentError, ValueError):  # label only: never fail startup over it
        return "unparseable"
    if u.get_backend_name() == "sqlite":
        name = u.database or ":memory:"
        return f"sqlite:{Path(name).name if name != ':memory:' else name}"
    host = u.host or "local"
    return f"{u.drivername}://{host}{f':{u.port}' if u.port else ''}/{u.database or ''}"


def port_in_use_reason(host: str, port: int) -> str | None:
    """Pre-bind check (no SO_REUSEADDR, so Windows reports a busy port too). None when bindable."""
    if port == 0:
        return None
    try:
        family, kind, proto, _, addr = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0]
        with socket.socket(family, kind, proto) as s:
            s.bind(addr)
    except OSError as exc:
        return f"{type(exc).__name__}"
    return None


def build_server_app(args: argparse.Namespace, *, repo_root: Path | None = None):
    """Testable seam: returns (fastapi_app, runtime_app). Caller closes runtime_app."""
    runtime_app = build_runtime(database_url=args.database_url, artifact_root=args.artifact_root,
                                fake=args.fake, ensure_db_schema=True)
    log_provider_readiness(runtime_app)
    extra_hosts = [] if is_loopback_host(args.host) else [args.host]
    api = create_app(runtime_app, run_runtime=not args.no_runtime, allowed_hosts=extra_hosts,
                     frontend_dir=resolve_frontend_dir(args, repo_root))
    return api, runtime_app


def uvicorn_kwargs(args: argparse.Namespace) -> dict:
    # log_config=None: uvicorn's own dictConfig would bypass our redacting handlers.
    kw = {"host": args.host, "port": args.port, "log_config": None}
    if args.log_level:
        kw["log_level"] = args.log_level
    return kw


def build_server(api, args: argparse.Namespace):
    """A ``uvicorn.Server`` wired like ``main`` (tests run it in a thread and stop it via ``should_exit``)."""
    import uvicorn
    return uvicorn.Server(uvicorn.Config(api, **uvicorn_kwargs(args)))


def log_banner(args: argparse.Namespace, api) -> None:
    served = bool(getattr(getattr(api, "state", None), "frontend_served", False))
    logger.info("StoryFlow API %s listening_on=%s:%s database=%s frontend=%s runtime=%s fake=%s",
                VERSION, args.host, args.port, database_label(args.database_url),
                "yes" if served else "no", "external" if args.no_runtime else "embedded",
                "yes" if args.fake else "no")


def schedule_browser_open(url: str, host: str, port: int, opener: Callable[[str], object] = webbrowser.open, *,
                          timeout: float = BROWSER_WAIT_SECONDS, stop: threading.Event | None = None) -> threading.Thread:
    """Daemon helper thread: waits until ``host:port`` accepts a connection, then calls ``opener(url)`` once.
    Best effort: an unreachable server, a timeout or a failing opener only logs; startup is never affected."""
    stop = stop or threading.Event()
    connect_host = "127.0.0.1" if host in ("0.0.0.0", "") else ("::1" if host == "::" else host.strip("[]"))

    def _run() -> None:
        waited = 0.0
        while waited < timeout and not stop.is_set():
            try:
                with socket.create_connection((connect_host, port), timeout=0.5):
                    break
            except OSError:
                stop.wait(0.1)
                waited += 0.1
        else:
            logger.info("browser not opened: server was not reachable in time")
            return
        try:
            opener(url)
        except Exception as exc:  # noqa: BLE001 - documented: opening a browser is best effort and must never crash the server
            logger.warning("could not open the browser (%s); open %s manually", type(exc).__name__, url)

    t = threading.Thread(target=_run, name="storyflow-open-browser", daemon=True)
    t.start()
    return t


def _fail(message: str) -> int:
    print(f"storyflow.api: {' '.join(message.split())[:400]}", file=sys.stderr)
    return 2


def _install_signals() -> Callable[[], None]:
    """Main thread only. uvicorn handles SIGINT/SIGTERM itself while serving and re-raises the captured
    signal afterwards, so SIGTERM must have a non-fatal handler (KeyboardInterrupt, caught in ``main``).
    Windows CTRL_BREAK (SIGBREAK) is not handled by uvicorn: route it to SIGINT so it stops gracefully."""
    if threading.current_thread() is not threading.main_thread():
        return lambda: None

    def _interrupt(_signum, _frame):
        raise KeyboardInterrupt

    def _break(_signum, _frame):
        signal.raise_signal(signal.SIGINT)

    previous = {signal.SIGTERM: signal.signal(signal.SIGTERM, _interrupt)}
    if hasattr(signal, "SIGBREAK"):
        previous[signal.SIGBREAK] = signal.signal(signal.SIGBREAK, _break)

    def restore() -> None:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return restore


def main(argv: list[str] | None = None, *, opener: Callable[[str], object] = webbrowser.open,
         repo_root: Path | None = None) -> int:
    args = parse_args(argv)
    try:
        handle = configure_logging(level=args.log_level, log_dir=args.log_dir, file=args.log_file)
    except (OSError, ValueError) as exc:
        return _fail(f"cannot set up logging: {type(exc).__name__}: {exc}. Check --log-dir / STORYFLOW_LOG_LEVEL.")
    try:
        return _serve(args, opener, repo_root)
    finally:
        handle.close()


def _serve(args: argparse.Namespace, opener, repo_root) -> int:
    if not is_loopback_host(args.host):
        if not args.allow_non_loopback:
            print(f"storyflow.api: refusing to bind non-loopback host {args.host!r} (the API has no "
                  "authentication); pass --allow-non-loopback to override.", file=sys.stderr)
            return 2
        logger.warning("binding %s: the API has NO authentication; anyone who can reach it can control workflows",
                       args.host)
    if not 0 <= args.port <= 65535:
        return _fail(f"invalid --port {args.port}; use 1-65535.")
    if port_in_use_reason(args.host, args.port) is not None:
        return _fail(f"cannot listen on {args.host}:{args.port} (port already in use or not permitted). Is another "
                     "StoryFlow running? Stop it or pass --port <other>.")
    try:
        api, runtime_app = build_server_app(args, repo_root=repo_root) if repo_root is not None \
            else build_server_app(args)
    except SchemaError as exc:
        return _fail(f"startup failed: {exc}")
    except (SQLAlchemyError, OSError, ValueError) as exc:
        return _fail(f"startup failed: {type(exc).__name__}: {exc}. Check --database-url / --artifact-root and "
                     "that the database is writable.")
    stop_browser = threading.Event()
    restore_signals = _install_signals()
    try:
        log_banner(args, api)
        if args.open_browser:
            shown = "127.0.0.1" if args.host in ("0.0.0.0", "::", "") else args.host
            schedule_browser_open(f"http://{shown}:{args.port}/", args.host, args.port, opener, stop=stop_browser)
        import uvicorn
        try:
            uvicorn.run(api, **uvicorn_kwargs(args))
        except KeyboardInterrupt:
            logger.info("interrupt received; stopping")  # signal re-raised by uvicorn after its graceful exit
    finally:
        stop_browser.set()
        restore_signals()
        host = getattr(getattr(getattr(api, "state", None), "container", None), "host", None)
        if host is not None and not host.stop():  # idempotent: normally already stopped by the lifespan
            logger.error("runtime thread still alive after shutdown timeout")
        runtime_app.close()
        logger.info("shutdown complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
