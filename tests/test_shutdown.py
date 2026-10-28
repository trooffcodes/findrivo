import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core import shutdown as shutdown_core  # noqa: E402


class ShutdownManagerTests(unittest.TestCase):
    def test_request_sets_registered_cancel_events(self):
        manager = shutdown_core.ShutdownManager(grace_seconds=5)
        event = threading.Event()
        manager.register_cancel_event(event, "test")
        manager.request()
        self.assertTrue(event.is_set())
        self.assertTrue(manager.is_shutting_down())
        self.assertFalse(manager.was_forced())

    def test_request_is_idempotent_until_second_press(self):
        manager = shutdown_core.ShutdownManager(grace_seconds=5)
        manager.request()
        self.assertFalse(manager.was_forced())
        manager.request()
        self.assertTrue(manager.was_forced())

    def test_cleanup_runs_and_isolates_errors(self):
        manager = shutdown_core.ShutdownManager(grace_seconds=5)
        calls = []
        manager.register_cleanup(lambda: calls.append("ok"))
        def boom():
            raise RuntimeError("cleanup explosion")
        manager.register_cleanup(boom)
        finished = manager.run_cleanup()
        self.assertTrue(finished)
        self.assertEqual(calls, ["ok"])  # the failing callback did not stop the rest

    def test_exit_code_is_130(self):
        manager = shutdown_core.ShutdownManager(grace_seconds=5)
        self.assertEqual(manager.exit_code(), 130)

    def test_request_during_shutdown_sets_events_immediately(self):
        manager = shutdown_core.ShutdownManager(grace_seconds=5)
        manager.request()
        late_event = threading.Event()
        manager.register_cancel_event(late_event, "late")
        self.assertTrue(late_event.is_set())


if __name__ == "__main__":
    unittest.main()
