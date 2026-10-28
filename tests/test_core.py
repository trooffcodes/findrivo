import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.circuit_breaker import CircuitBreaker  # noqa: E402
from core.pipeline import LeadGenPipeline  # noqa: E402
from config import Config  # noqa: E402
from methods.exporter import CSVLeadExporter, _csv_safe  # noqa: E402


class CircuitBreakerTests(unittest.TestCase):
    def test_half_open_allows_only_one_probe(self):
        breaker = CircuitBreaker("test", default_cooldown=0.01)
        breaker.record_failure(RuntimeError("boom"))
        time.sleep(0.02)
        self.assertTrue(breaker.is_available)
        self.assertFalse(breaker.is_available)
        breaker.record_success()
        self.assertTrue(breaker.is_available)

    def test_success_resets_state(self):
        breaker = CircuitBreaker("test", default_cooldown=60)
        breaker.record_failure(RuntimeError("boom"))
        self.assertFalse(breaker.is_available)
        breaker.record_success()
        self.assertTrue(breaker.is_available)


class ExporterSafetyTests(unittest.TestCase):
    def test_csv_formula_injection_is_neutralized(self):
        for value in ("=cmd|' /C calc'!A0", "+1+1", "-2+3", "@SUM(A1)"):
            self.assertTrue(_csv_safe(value).startswith("'"))

    def test_format_lead_escapes_company_name(self):
        row = CSVLeadExporter._format_lead(
            {"name": "=HYPERLINK(\"http://evil\")", "email": "a@example.org"},
            1,
        )
        self.assertTrue(row["company_name"].startswith("'="))

    def test_export_filename_rejects_path_separators(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("../evil.csv", "..\\evil.csv", "nested/evil.csv"):
                with self.assertRaises(ValueError):
                    CSVLeadExporter(output_dir=tmp, filename=name)

    def test_format_lead_handles_missing_numeric_values(self):
        row = CSVLeadExporter._format_lead(
            {"name": "Example", "load_time_seconds": None, "personalization_hooks": "hook"},
            1,
        )
        self.assertEqual(row["page_load_seconds"], "0.00")
        self.assertEqual(row["personalization_hook_1"], "hook")


class FakeLLM:
    def query(self, prompt, system_instruction=None, **kwargs):
        return json.dumps(
            {
                "industry": None,
                "location": "",
                "target_defects": "not-a-list",
                "search_queries": None,
                "target_count": 9999,
            }
        )


class PlannerValidationTests(unittest.TestCase):
    def test_plan_normalizes_hostile_llm_output(self):
        pipeline = LeadGenPipeline(llm_client=FakeLLM(), concurrency=1)
        plan = pipeline.plan("Dental Clinic in Austin, TX", target_count=3)
        self.assertIsInstance(plan["industry"], str)
        self.assertTrue(plan["industry"])
        self.assertIsInstance(plan["location"], str)
        self.assertTrue(plan["location"])
        self.assertIsInstance(plan["target_defects"], list)
        self.assertEqual(plan["target_count"], 3)

    def test_heuristic_planner(self):
        plan = LeadGenPipeline._heuristic_planner("Dental Clinic in Austin, TX", 7)
        self.assertEqual(plan["industry"], "Dental Clinic")
        self.assertEqual(plan["location"], "Austin, TX")
        self.assertEqual(plan["target_count"], 7)

    def test_target_count_is_clamped_to_configured_maximum(self):
        pipeline = LeadGenPipeline(llm_client=FakeLLM(), concurrency=1)
        plan = pipeline.plan("Dental Clinic in Austin, TX", target_count=10_000)
        self.assertEqual(plan["target_count"], Config.MAX_TARGET_COUNT)


class FakeExporter:
    def __init__(self, filename=None):
        self.rows = []

    def append_lead(self, lead):
        self.rows.append(lead)


class FakeDiscovery:
    def __init__(self, count=10):
        self.count = count

    def __call__(self, **kwargs):
        for index in range(self.count):
            yield {"name": f"Lead {index}"}


class PipelineCancellationTests(unittest.TestCase):
    def test_closing_generator_stops_producer_without_blocking(self):
        pipeline = LeadGenPipeline(llm_client=FakeLLM(), concurrency=1)
        lead = {
            "name": "Lead",
            "needs_redesign": True,
            "viability_score": 90,
            "audit_defects": [],
        }
        exporter = FakeExporter()
        with patch("core.pipeline.discover_businesses", new=FakeDiscovery(50)), patch(
            "core.pipeline.CSVLeadExporter", return_value=exporter
        ), patch.object(
            LeadGenPipeline, "_process_candidate", return_value=lead
        ):
            generator = pipeline.execute("Dental Clinic in Austin, TX", target_count=5)
            first = next(generator)
            self.assertEqual(first["name"], "Lead")
            generator.close()

        # The exporter received exactly the lead that was yielded.
        self.assertEqual(len(exporter.rows), 1)

    def test_pre_cancelled_run_yields_nothing(self):
        pipeline = LeadGenPipeline(llm_client=FakeLLM(), concurrency=1)
        cancel_event = threading.Event()
        cancel_event.set()
        with patch("core.pipeline.discover_businesses", new=FakeDiscovery(10)), patch(
            "core.pipeline.CSVLeadExporter", return_value=FakeExporter()
        ), patch.object(LeadGenPipeline, "_process_candidate", return_value={}):
            started = time.monotonic()
            results = list(
                pipeline.execute(
                    "Dental Clinic in Austin, TX",
                    target_count=5,
                    cancel_event=cancel_event,
                )
            )
            self.assertEqual(results, [])
            self.assertLess(time.monotonic() - started, 5.0)


if __name__ == "__main__":
    unittest.main()
