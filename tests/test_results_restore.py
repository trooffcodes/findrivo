import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from config import Config  # noqa: E402
from core import persistence  # noqa: E402
import app as app_module  # noqa: E402


def _fake_pipeline_generator(leads):
    def runner(*args, **kwargs):
        yield from leads
    return runner


class ResultsRestoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._results_patcher = patch.object(
            Config, "RESULTS_DIR", Path(self._tmp.name) / "jobs", create=True
        )
        self._results_patcher.start()

    def tearDown(self):
        self._results_patcher.stop()
        self._tmp.cleanup()

    def test_completed_results_are_restored_after_reload(self):
        leads = [
            {"name": "Biz A", "maps_link": "/maps/place/a", "email": "a@b.example",
             "phone": "(512) 555-0100", "needs_redesign": True},
            {"name": "Biz B", "maps_link": "/maps/place/b", "email": "", "needs_redesign": True},
            # Duplicate of the first lead (same identity) must not appear twice.
            {"name": "Biz A", "maps_link": "/maps/place/a"},
        ]
        with patch.object(Config, "ALLOW_UNAUTHENTICATED", True), patch.object(
            Config, "API_TOKEN", ""
        ), patch("app.pipeline") as fake_pipeline:
            fake_pipeline.execute = _fake_pipeline_generator(leads)
            client = app_module.app.test_client()
            start = client.post("/api/pipeline/start", json={"query": "Dental in Austin"})
            self.assertEqual(start.status_code, 200)

            # Wait for the background thread to finish persisting.
            deadline = time.monotonic() + 5
            status = None
            while time.monotonic() < deadline:
                latest = client.get("/api/results/latest")
                state = latest.get_json()
                status = state.get("status")
                if status == persistence.STATUS_COMPLETE:
                    break
                time.sleep(0.02)

            self.assertEqual(status, persistence.STATUS_COMPLETE)
            latest = client.get("/api/results/latest")
            data = latest.get_json()
            self.assertTrue(data["restored"])
            self.assertEqual(len(data["leads"]), 2)  # duplicates removed
            self.assertEqual(data["retention_hours"], Config.RESULTS_RETENTION_HOURS)

            # Also fetchable per task id.
            by_task = client.get(f"/api/results/{data['task_id']}")
            self.assertEqual(by_task.status_code, 200)

    def test_interruption_preserves_partial_results(self):
        leads = [{"name": "Partial Biz", "maps_link": "/maps/place/p", "needs_redesign": True}]

        def runner(*args, **kwargs):
            for lead in leads:
                yield lead
            # Simulate the client disconnecting mid-run: the cancel event set
            # by the stream's finally block interrupts the job.
            kwargs["cancel_event"].set()

        with patch.object(Config, "ALLOW_UNAUTHENTICATED", True), patch.object(
            Config, "API_TOKEN", ""
        ), patch("app.pipeline") as fake_pipeline:
            fake_pipeline.execute = runner
            client = app_module.app.test_client()
            start = client.post("/api/pipeline/start", json={"query": "Dental in Austin"})
            self.assertEqual(start.status_code, 200)
            task_id = start.get_json()["task_id"]

            # Consume the stream (then disconnect, like a browser reload).
            stream = client.get(f"/api/pipeline/stream/{task_id}")
            stream.close()

            deadline = time.monotonic() + 5
            state = None
            while time.monotonic() < deadline:
                state = app_module.ACTIVE_STREAMS.get(task_id, {})
                if not state:
                    break
                time.sleep(0.02)
            latest = client.get("/api/results/latest")
            data = latest.get_json()
            self.assertTrue(data["restored"])
            self.assertGreaterEqual(len(data["leads"]), 1)
            self.assertIn(data["status"], (persistence.STATUS_INTERRUPTED, persistence.STATUS_COMPLETE, persistence.STATUS_RUNNING))


if __name__ == "__main__":
    unittest.main()
