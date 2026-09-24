"""Runtime coexistence: who owns the scheduler loop when the API is up.

Ownership rules (documented contract)
-------------------------------------
* There is exactly ONE scheduler loop per database. Either
  - **embedded**: ``create_app(runtime_app, run_runtime=True)``; this module's ``RuntimeHost`` owns a single
    background thread that runs ``runtime.run_forever()`` on the SAME ``RuntimeApp`` (same orchestrator,
    registry and dispatcher the API services use), or
  - **external**: ``run_runtime=False``; the API is command/query only and a separate
    ``python -m storyflow.runtime --run`` process owns the loop (the DB queue/CAS guards make even an
    accidental second loop safe, but it is not a supported configuration).
* Startup: the FastAPI lifespan calls ``RuntimeHost.start()``; shutdown (uvicorn SIGINT/SIGTERM ->
  lifespan shutdown) calls ``RuntimeHost.stop()``. ``stop`` = ``request_stop`` (an Event) + ``join(timeout)``.
  It is idempotent; if the thread does not stop within the timeout ``stop`` returns False, logs an error and
  ``stop_timed_out`` stays True (a round is atomic, so a slow provider call is the only cause). The thread is a
  daemon so it can never keep the interpreter alive, but it is never silently abandoned.
* The host does NOT close the ``RuntimeApp`` (engine); whoever built it (``__main__`` or the test) does.
* The only broad ``except Exception`` here wraps the thread body: a crash of the loop must be recorded
  (``last_crash`` = exception class name only, never a message/path) instead of vanishing with the thread.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger("storyflow.api.host")


class RuntimeHost:
    def __init__(self, runtime_app, *, stop_timeout: float = 15.0):
        self.runtime_app = runtime_app
        self.stop_timeout = stop_timeout
        self._thread: threading.Thread | None = None
        self._cond = threading.Condition()
        self._iteration = 0
        self._errors = 0
        self._crash: str | None = None
        self.stop_timed_out = False

    # ------------------------------------------------------------------ state

    @property
    def running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    @property
    def thread(self) -> threading.Thread | None:
        return self._thread

    @property
    def iteration(self) -> int:
        with self._cond:
            return self._iteration

    @property
    def last_error_count(self) -> int:
        """Total per-workflow errors reported by the loop since start."""
        with self._cond:
            return self._errors

    @property
    def last_crash(self) -> str | None:
        with self._cond:
            return self._crash

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self.running:
            return
        runtime = self.runtime_app.runtime
        runtime.stop_event.clear()  # a stopped host can be started again
        self.stop_timed_out = False
        with self._cond:
            self._crash = None
        self._thread = threading.Thread(target=self._main, name="storyflow-runtime", daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = None) -> bool:
        """Idempotent. True when no runtime thread is alive afterwards."""
        t = self._thread
        if t is None:
            return True
        self.runtime_app.runtime.request_stop()
        t.join(self.stop_timeout if timeout is None else timeout)
        if t.is_alive():
            self.stop_timed_out = True
            logger.error("runtime thread did not stop within %.1fs", self.stop_timeout if timeout is None else timeout)
            return False
        self._thread = None
        with self._cond:
            self._cond.notify_all()
        return True

    def wait_iteration(self, timeout: float = 10.0) -> bool:
        """Block until one MORE loop iteration completes (event-driven, no polling). False on timeout or
        when the thread is not running."""
        with self._cond:
            target = self._iteration + 1
            return self._cond.wait_for(lambda: self._iteration >= target or not self.running, timeout) \
                and self._iteration >= target

    # ------------------------------------------------------------------ thread body

    def _on_iteration(self, report) -> None:
        with self._cond:
            self._iteration += 1
            self._errors += len(report.errors)
            self._cond.notify_all()

    def _main(self) -> None:
        try:
            self.runtime_app.runtime.run_forever(on_iteration=self._on_iteration)
        except Exception as exc:  # noqa: BLE001 - documented: record a loop crash instead of losing it with the thread
            logger.exception("runtime loop crashed")
            with self._cond:
                self._crash = type(exc).__name__
        finally:
            with self._cond:
                self._cond.notify_all()
