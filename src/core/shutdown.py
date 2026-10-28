"""
core/shutdown.py
Graceful Ctrl+C / SIGTERM handling shared by the CLI and web server modes.

Behavior on the first interrupt:
  - prints a clear shutdown banner,
  - signals every registered job to stop accepting new work,
  - runs registered cleanup callbacks (browser pool, HTTP sessions, ...) with
    exceptions are isolated per callback,
  - waits (bounded by SHUTDOWN_GRACE_SECONDS) for cleanup to settle and then
    exits with code 130.

A second interrupt forces an immediate exit and never raises uncontrolled
cleanup errors (all cleanup work is exception-guarded).
"""

import signal
import sys
import threading
import time
from typing import Callable, List, Optional

from config import Config
from core.logger import app_logger

_STATE_LOCK = threading.Lock()
_manager: Optional["ShutdownManager"] = None


class ShutdownManager:
    def __init__(self, grace_seconds: Optional[float] = None):
        self.grace_seconds = (
            Config.SHUTDOWN_GRACE_SECONDS if grace_seconds is None else grace_seconds
        )
        self._lock = threading.Lock()
        self._shutting_down = False
        self._forced = False
        self._cancel_events: List[threading.Event] = []
        self._cleanups: List[Callable[[], None]] = []
        self._press_count = 0

    def register_cancel_event(self, event: threading.Event, name: str = "") -> None:
        with self._lock:
            if self._shutting_down:
                event.set()
            self._cancel_events.append(event)

    def register_cleanup(self, cleanup: Callable[[], None]) -> None:
        with self._lock:
            self._cleanups.append(cleanup)

    def is_shutting_down(self) -> bool:
        with self._lock:
            return self._shutting_down

    def was_forced(self) -> bool:
        with self._lock:
            return self._forced

    def press_count(self) -> int:
        with self._lock:
            return self._press_count

    def request(self) -> None:
        """Record the shutdown request (idempotent, non-blocking)."""
        with self._lock:
            self._press_count += 1
            if self._shutting_down:
                if self._press_count >= 2:
                    self._forced = True
                return
            self._shutting_down = True
            for event in list(self._cancel_events):
                event.set()

    def run_cleanup(self, timeout: Optional[float] = None) -> bool:
        """Stop scheduling, run cleanup callbacks, return True when finished.

        Never raises: each cleanup callback is isolated and logged separately.
        """
        started = time.monotonic()
        deadline = started + (self.grace_seconds if timeout is None else timeout)

        with self._lock:
            events = list(self._cancel_events)
        for event in events:
            event.set()

        with self._lock:
            cleanups = list(self._cleanups)
        for cleanup in cleanups:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self.was_forced():
                app_logger.warning("Skipping cleanup %s (deadline reached)", getattr(cleanup, "__qualname__", cleanup))
                continue
            try:
                cleanup()
            except Exception as err:  # noqa: BLE001 - shutdown must never crash
                app_logger.error("Cleanup callback error: %s", err)

        return time.monotonic() < deadline

    def exit_code(self) -> int:
        return 130 if not self.was_forced() else 131


def get_manager() -> ShutdownManager:
    global _manager
    with _STATE_LOCK:
        if _manager is None:
            _manager = ShutdownManager()
        return _manager


def is_shutting_down() -> bool:
    return get_manager().is_shutting_down()


SHUTDOWN_BANNER = "Shutdown requested. Closing workers and browser resources..."


def _handle_first_press(manager: ShutdownManager, mode: str) -> None:
    if manager.is_shutting_down():
        # Repeated interrupt while cleanup is already in progress: honor the
        # user's intent by leaving immediately, without re-running cleanup.
        print("Forcing immediate exit (interrupt received during shutdown).", flush=True)
        manager.request()  # marks as forced via idempotent path
        _force_exit_now(manager)
        return

    print(f"\n{SHUTDOWN_BANNER}", flush=True)
    manager.request()

    finished = manager.run_cleanup()
    if not finished and not manager.was_forced():
        app_logger.warning("Graceful shutdown did not complete within grace window.")
        print("Some background work may not have finished cleanly.", flush=True)

    app_logger.info("Shutdown complete (mode=%s, forced=%s)", mode, manager.was_forced())


def _force_exit_now(manager: ShutdownManager) -> None:
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        import os

        os._exit(manager.exit_code())


def install_handlers(mode: str) -> ShutdownManager:
    """Install Ctrl+C handling.

    mode="cli":
      The first Ctrl+C raises KeyboardInterrupt so the foreground run loop can
      catch it where its results state lives; the handler itself only records
      the request. A second press exits immediately.
    mode="server":
      The Flask dev server occupies the main thread and would not unwind
      cleanly, so the handler runs cleanup itself right before exiting.
    """
    manager = get_manager()

    def _signal_handler(signum, frame):  # noqa: ANN001 - signal handler signature
        if mode == "cli":
            # First press: record the request and let the CLI loop unwind
            # where it can flush partial results itself. Repeated press:
            # leave immediately, never re-running cleanup.
            if manager.is_shutting_down():
                _force_exit_now(manager)
            manager.request()
            raise KeyboardInterrupt

        _handle_first_press(manager, mode)

    signal.signal(signal.SIGINT, _signal_handler)
    try:
        signal.signal(signal.SIGTERM, _signal_handler)
    except (ValueError, AttributeError):
        pass  # Not available on every platform/thread context.

    return manager
