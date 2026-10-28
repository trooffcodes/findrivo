"""Focused tests for clear-start maintenance (data + logs)."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from config import Config  # noqa: E402
from core import maintenance, persistence  # noqa: E402


class MaintenanceDataTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.results_dir = base / "jobs"
        self.exports_dir = base / "exports"
        self.progress = base / "discovery_progress.json"
        self._patchers = [
            patch.object(Config, "RESULTS_DIR", self.results_dir, create=True),
            patch.object(Config, "PROGRESS_FILE", str(self.progress), create=True),
            patch.object(Config, "EXPORT_DIR", self.exports_dir, create=True),
        ]
        for patcher in self._patchers:
            patcher.start()

    def tearDown(self):
        for patcher in self._patchers:
            patcher.stop()
        self._tmp.cleanup()

    def test_clear_saved_data_removes_jobs_and_progress(self):
        persistence.save_job({"task_id": "abc", "status": persistence.STATUS_COMPLETE})
        persistence.save_job({"task_id": "def", "status": persistence.STATUS_RUNNING})
        self.progress.write_text("{}", encoding="utf-8")
        (self.progress.parent / (str(self.progress) + ".lock")).write_text("x", encoding="utf-8")

        result = maintenance.clear_saved_data()

        self.assertEqual(result["errors"], [])
        self.assertFalse(self.progress.exists())
        self.assertEqual(list(self.results_dir.glob("job_*.json")), [])

    def test_clear_saved_data_never_touches_exports(self):
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        export = self.exports_dir / "findrivo_leads_test.csv"
        export.write_text("company_name\n", encoding="utf-8")

        maintenance.clear_saved_data()

        self.assertTrue(export.exists())

    def test_clear_saved_data_handles_missing_dirs(self):
        result = maintenance.clear_saved_data()
        self.assertEqual(result["errors"], [])


class MaintenanceLogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self._tmp.name) / "logs"
        self.log_dir.mkdir(parents=True)
        self._log_patch = patch.object(Config, "LOG_DIR", self.log_dir, create=True)
        self.addCleanup(self._log_patch.stop)
        self._log_patch.start()

    def tearDown(self):
        self._tmp.cleanup()

    def test_logs_are_truncated_not_deleted(self):
        app_log = self.log_dir / "app.log"
        app_log.write_text("secret-ish app content" * 100, encoding="utf-8")
        backup = self.log_dir / "app.log.1"
        backup.write_text("older content", encoding="utf-8")

        result = maintenance.clear_logs()

        self.assertEqual(result["errors"], [])
        self.assertIn("app.log", result["cleared"])
        for path in (app_log, backup):
            self.assertTrue(path.exists())
            self.assertEqual(path.read_text(encoding="utf-8"), "")

    def test_non_log_files_are_untouched(self):
        other = self.log_dir / "notes.txt"
        other.write_text("keep me", encoding="utf-8")

        maintenance.clear_logs()

        self.assertEqual(other.read_text(encoding="utf-8"), "keep me")

    def test_missing_log_dir_is_clean(self):
        with patch.object(Config, "LOG_DIR", self.log_dir / "missing", create=True):
            result = maintenance.clear_logs()
        self.assertEqual(result, {"cleared": [], "errors": []})


if __name__ == "__main__":
    unittest.main()
