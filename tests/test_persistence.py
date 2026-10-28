import json
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
from methods.exporter import build_export_filename, CSVLeadExporter  # noqa: E402


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        results_dir = Path(self._tmp.name) / "jobs"
        self._patcher = patch.object(Config, "RESULTS_DIR", results_dir, create=True)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmp.cleanup()

    def test_save_and_load_round_trip(self):
        persistence.save_job({
            "task_id": "abc-123",
            "status": persistence.STATUS_RUNNING,
            "leads": [{"name": "Biz", "maps_link": "/maps/place/biz"}],
        })
        state = persistence.load_job("abc-123")
        self.assertIsNotNone(state)
        self.assertEqual(state["status"], persistence.STATUS_RUNNING)
        self.assertEqual(state["leads"][0]["name"], "Biz")
        self.assertIn("updated_at", state)

    def test_latest_job_returns_most_recent(self):
        persistence.save_job({"task_id": "job-old", "status": persistence.STATUS_COMPLETE})
        time.sleep(0.01)
        persistence.save_job({"task_id": "job-new", "status": persistence.STATUS_RUNNING})
        latest = persistence.latest_job()
        self.assertEqual(latest["task_id"], "job-new")

    def test_invalid_task_ids_are_rejected(self):
        self.assertIsNone(persistence.save_job({"task_id": "../evil"}))
        self.assertIsNone(persistence.load_job("../evil"))

    def test_expired_snapshots_are_pruned(self):
        persistence.save_job({"task_id": "expiring", "status": persistence.STATUS_COMPLETE})
        path = next((Path(Config.RESULTS_DIR)).glob("job_expiring.json"))
        # Rewind updated_at beyond retention.
        data = json.loads(path.read_text(encoding="utf-8"))
        data["updated_at"] = time.time() - (Config.RESULTS_RETENTION_HOURS + 1) * 3600
        path.write_text(json.dumps(data), encoding="utf-8")

        removed = persistence.prune_expired()
        self.assertEqual(removed, 1)
        self.assertFalse(path.exists())
        self.assertIsNone(persistence.load_job("expiring"))

    def test_deduplicate_leads_by_identity(self):
        leads = [
            {"name": "Biz", "maps_link": "/maps/place/biz"},
            {"name": "Biz2", "maps_link": "/maps/place/biz"},
            {"name": "Other", "maps_link": "/maps/place/other"},
            {"name": "NoLink", "address": "x", "phone": "1"},
        ]
        deduped = persistence.deduplicate_leads(leads)
        self.assertEqual(len(deduped), 3)
        self.assertEqual({l["name"] for l in deduped}, {"Biz", "Other", "NoLink"})


class ExportFilenameTests(unittest.TestCase):
    def test_descriptive_export_filename(self):
        name = build_export_filename("Dental Clinics in Austin, TX")
        self.assertTrue(name.startswith("findrivo_leads_dental-clinics-in-austin-tx_"))
        self.assertTrue(name.endswith(".csv"))
        self.assertNotIn("/", name)
        self.assertNotIn(" ", name)

    def test_empty_label_falls_back_to_campaign(self):
        name = build_export_filename("   ")
        self.assertIn("findrivo_leads_campaign_", name)

    def test_exporter_rejects_path_separators(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("..\\evil.csv", "nested/evil.csv", "findrivo\\evil.csv"):
                with self.assertRaises(ValueError):
                    CSVLeadExporter(output_dir=tmp, filename=name)

    def test_windows_path_separator_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                CSVLeadExporter(output_dir=tmp, filename="a\\b.csv")


class ExportFieldTests(unittest.TestCase):
    def test_new_fields_present_in_row(self):
        lead = {
            "name": "Biz",
            "email": "a@b.example",
            "phone": "(512) 555-0100",
            "extraction_confidence": 80,
            "confidence_level": "high",
            "opportunity_score": 70,
            "outreach_suitability": "ready",
            "evidence_summary": "email:verified",
            "opportunity_reasons": ["r1", "r2"],
            "data_sources": "google_maps",
            "response_time": {"seconds": 0.5, "basis": "median_of_2_http_get_durations", "samples": 2},
        }
        row = CSVLeadExporter._format_lead(lead, 1)
        self.assertEqual(row["extraction_confidence"], "80")
        self.assertEqual(row["confidence_level"], "high")
        self.assertEqual(row["opportunity_score"], "70")
        self.assertEqual(row["opportunity_reasons"], "r1; r2")
        self.assertEqual(row["outreach_suitability"], "ready")
        self.assertEqual(row["evidence_summary"], "email:verified")
        self.assertEqual(row["response_time_basis"], "median_of_2_http_get_durations")
        self.assertEqual(row["response_time_samples"], "2")
        self.assertEqual(row["page_load_seconds"], "0.50")

    def test_missing_values_stay_empty(self):
        row = CSVLeadExporter._format_lead({"name": "Biz", "ssl_days_remaining": None}, 1)
        self.assertEqual(row["ssl_days_remaining"], "")
        self.assertEqual(row["primary_email"], "")
        self.assertEqual(row["response_time_basis"], "")

    def test_zero_values_are_preserved(self):
        row = CSVLeadExporter._format_lead({"name": "Biz", "viability_score": 0}, 1)
        self.assertEqual(row["redesign_viability_score"], "0")


if __name__ == "__main__":
    unittest.main()
