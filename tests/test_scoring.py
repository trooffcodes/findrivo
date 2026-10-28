import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.scoring import (  # noqa: E402
    EvidenceTracker,
    INFERRED,
    UNVERIFIED,
    VERIFIED,
    extraction_confidence,
    opportunity_score,
    outreach_suitability,
    should_browser_fallback,
)


class _FakeConfig:
    BROWSER_FALLBACK_ENABLED = True
    BROWSER_FALLBACK_MIN_RATING = 3.5
    BROWSER_FALLBACK_MIN_REVIEWS = 10


class ExtractionConfidenceTests(unittest.TestCase):
    def test_missing_evidence_cannot_score_high(self):
        tracker = EvidenceTracker()
        score, level = extraction_confidence(tracker)
        self.assertEqual(score, 0)
        self.assertEqual(level, "low")

    def test_extraction_completing_does_not_mean_confidence_100(self):
        # A "successful" extraction that produced only inferred values must
        # never reach a perfect score.
        tracker = EvidenceTracker()
        for field in ("name", "website", "phone", "email", "address", "description"):
            tracker.claim(field, "value", INFERRED, "llm_inference")
        score, level = extraction_confidence(tracker)
        self.assertLess(score, 50)
        self.assertEqual(level, "low")

    def test_verified_primary_sources_score_high_but_capped(self):
        tracker = EvidenceTracker()
        for field, value in (
            ("name", "Biz"),
            ("website", "https://biz.example"),
            ("phone", "(512) 555-0100"),
            ("email", "hi@biz.example"),
            ("address", "123 Main St"),
            ("description", "Best biz"),
        ):
            tracker.claim(field, value, VERIFIED, "google_maps")
        score, level = extraction_confidence(tracker)
        self.assertGreaterEqual(score, 75)
        self.assertEqual(level, "high")
        self.assertLessEqual(score, 90)  # never presented as absolute certainty

    def test_weaker_values_do_not_overwrite_confirmed_ones(self):
        tracker = EvidenceTracker()
        tracker.claim("phone", "(512) 555-0100", VERIFIED, "google_maps", {"phone": "(512) 555-0100"})
        tracker.claim("phone", "(713) 000-0000", UNVERIFIED, "directory", {"phone": "(512) 555-0100"})
        self.assertEqual(tracker.field_status["phone"], VERIFIED)
        self.assertIn("(713) 000-0000", tracker.conflict_list()["phone"])

    def test_verified_absence_counts_as_knowledge(self):
        tracker = EvidenceTracker()
        tracker.claim_absence("website", "google_maps")
        self.assertEqual(tracker.field_status["website"], VERIFIED)
        self.assertEqual(tracker.field_sources["website"], "google_maps:absence")


class OpportunityScoreTests(unittest.TestCase):
    def _absent_website_lead(self,**overrides):
        lead = {
            "name": "Biz",
            "website": "",
            "rating": 4.7,
            "reviews": 120,
            "is_live": False,
        }
        lead.update(overrides)
        return lead

    def test_verified_absence_with_strong_reputation_is_high(self):
        tracker = EvidenceTracker()
        tracker.claim_absence("website", "google_maps")
        score, reasons = opportunity_score(self._absent_website_lead(), tracker)
        self.assertGreaterEqual(score, 60)
        self.assertTrue(any("No official website" in r for r in reasons))

    def test_unverified_absence_is_penalized(self):
        tracker = EvidenceTracker()
        tracker.claim("website", "", UNVERIFIED, "google_maps")
        score, _ = opportunity_score({"name": "Biz", "website": "", "rating": 4.7, "reviews": 120}, tracker)
        self.assertLess(score, 40)

    def test_observed_defects_drive_website_opportunity(self):
        tracker = EvidenceTracker()
        tracker.claim("website", "https://biz.example", UNVERIFIED, "google_maps")
        tracker.claim("website", "https://biz.example", VERIFIED, "audit_observed")
        lead = {
            "name": "Biz",
            "website": "https://biz.example",
            "is_live": True,
            "rating": 4.6,
            "reviews": 50,
            "design_verdict": "outdated",
            "primary_defects": ["No SSL", "No viewport meta"],
            "is_mobile_viewport": False,
            "has_ssl": False,
            "load_time_seconds": 0.5,
        }
        score, reasons = opportunity_score(lead, tracker)
        self.assertGreaterEqual(score, 55)
        self.assertTrue(any("observed" in r.lower() for r in reasons))

    def test_minor_fixes_alone_do_not_create_opportunity(self):
        tracker = EvidenceTracker()
        tracker.claim("website", "https://biz.example", VERIFIED, "audit_observed")
        lead = {
            "name": "Biz",
            "website": "https://biz.example",
            "is_live": True,
            "design_verdict": "passable",
            "primary_defects": ["No favicon link detected"],
            "is_mobile_viewport": True,
            "has_ssl": True,
            "load_time_seconds": 0.4,
        }
        score, reasons = opportunity_score(lead, tracker)
        self.assertLess(score, 40)
        self.assertTrue(any("functional and modern" in r for r in reasons))

    def test_unreliable_response_time_is_ignored(self):
        tracker = EvidenceTracker()
        tracker.claim("website", "https://biz.example", VERIFIED, "audit_observed")
        lead = {
            "name": "Biz",
            "website": "https://biz.example",
            "is_live": True,
            "design_verdict": "passable",
            "load_time_seconds": 8.0,
        }
        meta = {"samples": 1, "reliable": False, "inconclusive": True}
        score, reasons = opportunity_score(lead, tracker, meta)
        self.assertFalse(any("Median HTTP" in r for r in reasons))
        self.assertTrue(any("inconclusive" in r for r in reasons))

    def test_single_sample_slow_time_never_reaches_cap(self):
        tracker = EvidenceTracker()
        tracker.claim("website", "https://biz.example", VERIFIED, "audit_observed")
        lead = {
            "name": "Biz",
            "website": "https://biz.example",
            "is_live": True,
            "design_verdict": "passable",
            "load_time_seconds": 12.0,
        }
        meta = {"samples": 1, "reliable": False, "inconclusive": True}
        score, _ = opportunity_score(lead, tracker, meta)
        self.assertLessEqual(score, 40)


class OutreachSuitabilityTests(unittest.TestCase):
    def test_ready_with_verified_contact_and_facts(self):
        tracker = EvidenceTracker()
        tracker.claim("phone", "(512) 555-0100", VERIFIED, "google_maps")
        tracker.claim("website", "https://biz.example", VERIFIED, "audit_observed")
        suitability, note = outreach_suitability(
            {"phone": "(512) 555-0100", "website": "https://biz.example", "primary_defects": ["No SSL"]},
            tracker,
        )
        self.assertEqual(suitability, "ready")
        self.assertIn("Verified contact", note)

    def test_unverified_contact_needs_verification(self):
        tracker = EvidenceTracker()
        tracker.claim("email", "a@b.example", INFERRED, "llm_inference")
        suitability, _ = outreach_suitability({"email": "a@b.example"}, tracker)
        self.assertEqual(suitability, "needs_verification")

    def test_nothing_verified_is_insufficient(self):
        tracker = EvidenceTracker()
        suitability, _ = outreach_suitability({}, tracker)
        self.assertEqual(suitability, "insufficient")


class BrowserFallbackGateTests(unittest.TestCase):
    def _promising_lead(self, **overrides):
        lead = {
            "name": "Biz",
            "website": "https://biz.example",
            "is_live": True,
            "rating": 4.6,
            "reviews": 80,
            "email": "",
            "phone": "(512) 555-0100",
            "contact_form": "",
            "http_extraction_attempted": True,
            "pages_crawled": ["/"],
        }
        lead.update(overrides)
        return lead

    def test_complete_lead_never_triggers_fallback(self):
        ok, reason = should_browser_fallback(
            self._promising_lead(email="hi@biz.example", contact_form="/contact"),
            _FakeConfig(),
        )
        self.assertFalse(ok)
        self.assertIn("already extracted", reason)

    def test_promising_lead_with_missing_email_triggers_fallback(self):
        ok, reason = should_browser_fallback(self._promising_lead(), _FakeConfig())
        self.assertTrue(ok)
        self.assertIn("email", reason)

    def test_weak_leads_do_not_trigger_fallback(self):
        ok, _ = should_browser_fallback(
            self._promising_lead(rating=2.1, reviews=3),
            _FakeConfig(),
        )
        self.assertFalse(ok)

    def test_no_website_never_triggers_fallback(self):
        ok, _ = should_browser_fallback(self._promising_lead(website="", is_live=False), _FakeConfig())
        self.assertFalse(ok)

    def test_optional_fields_alone_do_not_trigger_fallback(self):
        ok, _ = should_browser_fallback(
            self._promising_lead(email="hi@biz.example", phone="(512) 555-0100", contact_form="/contact"),
            _FakeConfig(),
        )
        self.assertFalse(ok)

    def test_disabled_feature_stops_all_fallback(self):
        ok, reason = should_browser_fallback(self._promising_lead(), _FakeConfig())
        ok_disabled, reason_disabled = should_browser_fallback(
            self._promising_lead(), type("_Off", (_FakeConfig,), {"BROWSER_FALLBACK_ENABLED": False})()
        )
        self.assertTrue(ok)
        self.assertFalse(ok_disabled)
        self.assertIn("disabled", reason_disabled)


if __name__ == "__main__":
    unittest.main()
