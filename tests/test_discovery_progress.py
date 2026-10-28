import inspect
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from methods.discovery import lead_discovery  # noqa: E402


class ProgressStateTests(unittest.TestCase):
    def test_progress_is_merged_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "progress.json")
            lead_discovery._save_progress_atomic(
                path, {"processed_urls": ["a"], "completed_queries": ["q1"]}
            )
            lead_discovery._save_progress_atomic(
                path, {"processed_urls": ["b"], "completed_queries": ["q2"]}
            )
            loaded = lead_discovery._load_progress(path)
            self.assertEqual(loaded["processed_urls"], ["a", "b"])
            self.assertEqual(loaded["completed_queries"], ["q1", "q2"])
            self.assertFalse(os.path.exists(path + ".lock"))

    def test_unreadable_progress_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "progress.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            self.assertEqual(
                lead_discovery._load_progress(path),
                {"processed_urls": [], "completed_queries": []},
            )

    def test_resume_is_opt_in_by_default(self):
        signature = inspect.signature(lead_discovery.discover_businesses)
        self.assertFalse(signature.parameters["resume"].default)


if __name__ == "__main__":
    unittest.main()
