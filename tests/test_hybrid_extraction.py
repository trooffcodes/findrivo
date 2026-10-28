import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.pipeline import LeadGenPipeline  # noqa: E402
from methods.audit import network as audit_network  # noqa: E402


class FakeDiscovery:
    """Mimics the discovery generator with configurable raw leads."""
    def __init__(self, leads):
        self.leads = leads

    def __call__(self, **kwargs):
        yield from self.leads


class _FakeLLM:
    def query(self, prompt, system_instruction=None, **kwargs):
        return json.dumps({"hook": "h", "service_angle": "a"})


class FakeExporter:
    def __init__(self, filename=None):
        self.rows = []

    def append_lead(self, lead):
        self.rows.append(lead)


def fake_audit(overrides):
    base = {
        "is_live": True,
        "has_ssl": True,
        "is_mobile_viewport": True,
        "load_time_seconds": 0.4,
        "response_time": {"seconds": 0.4, "samples": 2, "basis": "median_of_2_http_get_durations",
                          "inconclusive": False, "reliable": True},
        "issues_found": [],
        "redesign_qualification": {
            "needs_redesign": False, "viability_score": 10, "confidence_score": 30,
            "design_verdict": "passable", "primary_defects": [],
        },
        "ssl_details": {"days_remaining": 90},
    }
    base.update(overrides)
    return base


class FakeExtractorResult:
    def __init__(self, emails=(), phones=(), forms=(), description="", pages=("/")):
        contacts = MagicMock()
        contacts.emails = list(emails)
        contacts.phone_numbers = list(phones)
        contacts.contact_forms = list(forms)
        self.contacts = contacts
        self.description = description
        self.pages_crawled = list(pages)


class FakePersonalizer:
    def analyze_company(self, website, extraction_res):
        b = MagicMock()
        b.one_liner = "Local provider"
        b.core_value_proposition = "Value"
        b.personalization_hooks = ["Hook 1", "Hook 2"]
        b.suggested_outreach_angle = "Angle"
        return b


class HybridExtractionTests(unittest.TestCase):
    """Pipeline behavior with mock network/browser: gate and merge semantics."""

    def _run(self, raw_leads, audit_result, extractor_result, browser_result, fallback_applied=None):
        pipeline = LeadGenPipeline(llm_client=_FakeLLM(), concurrency=1)
        with patch("core.pipeline.discover_businesses", new=FakeDiscovery(raw_leads)), patch(
            "core.pipeline.CSVLeadExporter", return_value=FakeExporter()
        ), patch("core.pipeline.audit_website", return_value=audit_result), patch.object(
            pipeline, "extractor"
        ) as fake_extractor, patch.object(
            pipeline, "personalizer", new=FakePersonalizer()
        ), patch("core.pipeline.safe_browser_fallback", return_value=browser_result) as fallback:
            fake_extractor.extract.return_value = extractor_result
            leads = list(pipeline.execute("Dental Clinic in Austin, TX", target_count=5, only_qualified_redesigns=False))
            return leads, fallback

    def test_complete_lead_never_triggers_browser_fallback(self):
        raw = [{"name": "Biz", "website": "https://biz.example", "listed_website": "https://biz.example",
                "phone": "(512) 555-0100", "address": "1 Main St", "rating": 4.6, "reviews": 40,
                "search_location": "Austin, TX"}]
        extraction = FakeExtractorResult(emails=["a@biz.example", "b@biz.example"], phones=[], forms=["/contact"])
        leads, fallback = self._run(raw, fake_audit({}), extraction, None)
        self.assertEqual(len(leads), 1)
        fallback.assert_not_called()
        lead = leads[0]
        self.assertEqual(lead["email"], "a@biz.example")
        self.assertEqual(lead["extraction_confidence"], lead["confidence_score"])
        self.assertIn("official_website_http", lead["data_sources"])
        self.assertNotIn("browser_rendered", lead["data_sources"])

    def test_promising_lead_with_missing_contacts_triggers_fallback(self):
        raw = [{"name": "Biz", "website": "https://biz.example", "listed_website": "https://biz.example",
                "phone": "", "address": "1 Main St", "rating": 4.8, "reviews": 90,
                "search_location": "Austin, TX"}]
        extraction = FakeExtractorResult(emails=[], phones=[], forms=[])
        browser = {"emails": ["found@biz.example"], "phone_numbers": ["(713) 555-0000"],
                   "contact_forms": [], "description": "", "socials": [], "pages_crawled": ["x"]}
        leads, fallback = self._run(raw, fake_audit({}), extraction, browser)
        self.assertEqual(len(leads), 1)
        fallback.assert_called_once()
        lead = leads[0]
        self.assertTrue(lead.get("browser_extraction_used"))
        self.assertEqual(lead["email"], "found@biz.example")
        self.assertEqual(lead["phone"], "(713) 555-0000")
        self.assertIn("browser_rendered", lead["data_sources"])

    def test_weak_leads_do_not_trigger_fallback(self):
        raw = [{"name": "WeakBiz", "website": "https://weak.example", "listed_website": "https://weak.example",
                "phone": "", "address": "", "rating": 2.0, "reviews": 2,
                "search_location": "Austin, TX"}]
        leads, fallback = self._run(raw, fake_audit({}), FakeExtractorResult(), None)
        fallback.assert_not_called()

    def test_conflicting_values_are_recorded_not_overwritten(self):
        raw = [{"name": "Biz", "website": "https://biz.example", "listed_website": "https://biz.example",
                "phone": "(512) 555-0100", "address": "", "rating": 4.6, "reviews": 50,
                "search_location": "Austin, TX"}]
        extraction = FakeExtractorResult(emails=[], phones=["(713) 555-9999"], forms=[])
        leads, _ = self._run(raw, fake_audit({}), extraction, None)
        lead = leads[0]
        self.assertEqual(lead["phone"], "(512) 555-0100")  # confirmed value kept
        self.assertTrue(lead.get("field_conflicts", {}).get("phone"))

    def test_no_website_lead_uses_verified_absence(self):
        raw = [{"name": "NoSiteBiz", "website": "", "listed_website": "",
                "phone": "(512) 555-0100", "address": "2 Main St", "rating": 4.9, "reviews": 300,
                "search_location": "Austin, TX"}]
        pipeline = LeadGenPipeline(llm_client=_FakeLLM(), concurrency=1)
        with patch("core.pipeline.discover_businesses", new=FakeDiscovery(raw)), patch(
            "core.pipeline.CSVLeadExporter", return_value=FakeExporter()
        ), patch("core.pipeline.audit_website") as audit_mock:
            leads = list(pipeline.execute("Dental Clinic in Austin, TX", target_count=1,
                                          only_qualified_redesigns=False))
        audit_mock.assert_not_called()
        lead = leads[0]
        self.assertEqual(lead["evidence"].get("website"), "verified")  # verified absence
        self.assertEqual(lead["design_verdict"], "no-website")
        self.assertGreater(lead["opportunity_score"], 40)
        self.assertLessEqual(lead["extraction_confidence"], 90)


class ResponseTimeSampleTests(unittest.TestCase):
    def test_second_sample_used_for_median(self):
        responses = [
            MagicMock(status_code=200, text="<html>a</html>"),
            MagicMock(status_code=200, text="<html>b</html>"),
        ]
        delays = [0.3, 0.9]
        calls = {"n": 0}
        def fake_safe_request(session, method, url, **kwargs):
            d = delays[calls["n"]]
            calls["n"] += 1
            responses[calls["n"] - 1].elapsed = d
            return responses[calls["n"] - 1]
        with patch("methods.audit.network.safe_request", side_effect=fake_safe_request), patch(
            "methods.audit.network.perf_logger"
        ), patch("methods.audit.network.time.perf_counter",
                 side_effect=[0.0, 0.3, 0.3, 1.2]):
            html, load_time, status, err, meta = audit_network.fetch_page_samples(
                "https://example.com", MagicMock(), max_samples=2)
        self.assertEqual(status, 200)
        self.assertEqual(meta["samples"], 2)
        self.assertEqual(meta["basis"], "median_of_2_http_get_durations")
        self.assertEqual(load_time, 0.6)
        self.assertTrue(meta["reliable"])
        self.assertFalse(meta["inconclusive"])

    def test_failed_first_sample_not_retried(self):
        import requests as _requests
        calls = {"n": 0}
        def fake_safe_request(session, method, url, **kwargs):
            calls["n"] += 1
            raise _requests.exceptions.ConnectionError("refused")
        with patch("methods.audit.network.safe_request", side_effect=fake_safe_request):
            html, load_time, status, err, meta = audit_network.fetch_page_samples(
                "https://example.com", MagicMock(), max_samples=2)
        self.assertIsNone(html)
        self.assertEqual(calls["n"], 1)
        self.assertTrue(meta["inconclusive"])
        self.assertFalse(meta["reliable"])
        self.assertEqual(load_time, 0.0)

    def test_single_sample_marked_not_reliable(self):
        calls = {"n": 0}
        def fake_safe_request(session, method, url, **kwargs):
            calls["n"] += 1
            return MagicMock(status_code=200, text="<html>x</html>")
        with patch("methods.audit.network.safe_request", side_effect=fake_safe_request), patch(
            "methods.audit.network.time.perf_counter", side_effect=[0.0, 0.5]
        ):
            html, load_time, status, err, meta = audit_network.fetch_page_samples(
                "https://example.com", MagicMock(), max_samples=1)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(meta["samples"], 1)
        self.assertEqual(meta["basis"], "single_http_get_duration")
        self.assertFalse(meta["reliable"])
        self.assertFalse(meta["inconclusive"])


if __name__ == "__main__":
    unittest.main()
