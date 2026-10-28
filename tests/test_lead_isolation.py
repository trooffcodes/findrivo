"""Focused tests: lead isolation, social host matching, interruption recovery."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from config import Config  # noqa: E402
from core import persistence  # noqa: E402
from core.pipeline import _merge_fallback_result  # noqa: E402
from core.scoring import EvidenceTracker  # noqa: E402
from methods.extraction.models import SocialProfile  # noqa: E402
from methods.extraction.parsers.social_parser import SocialParser  # noqa: E402


class MergeFallbackResultTests(unittest.TestCase):
    """Browser fallback output must never crash a whole lead record."""

    def _merge(self, fallback_socials):
        lead = {"name": "Test Biz"}
        evidence = EvidenceTracker()
        emails, phones = [], []
        _merge_fallback_result(
            lead,
            {
                "emails": ["info@test.biz"],
                "phone_numbers": ["+1 512 555 0100"],
                "contact_forms": ["https://test.biz/contact"],
                "description": "A test business",
                "socials": fallback_socials,
            },
            evidence,
            emails,
            phones,
        )
        return lead, evidence

    def test_dict_socials_merge(self):
        lead, _ = self._merge([{"platform": "Facebook", "url": "https://facebook.com/testbiz"}])
        self.assertEqual(lead["facebook_url"], "https://facebook.com/testbiz")
        self.assertEqual(lead["email"], "info@test.biz")
        self.assertEqual(lead["contact_form"], "https://test.biz/contact")

    def test_pydantic_socials_no_longer_crash(self):
        # Regression: browser fallback used to return raw SocialProfile models
        # and _merge_fallback_result crashed with AttributeError, losing the
        # entire lead after all processing had already completed.
        model = SocialProfile(platform="Instagram", url="https://instagram.com/testbiz", handle="testbiz")
        lead, _ = self._merge([model])
        self.assertEqual(lead["instagram_url"], "https://instagram.com/testbiz")
        self.assertEqual(lead["email"], "info@test.biz")

    def test_existing_values_not_overwritten(self):
        lead, _ = self._merge([])
        lead0 = {"name": "Biz", "email": "primary@first.com"}
        evidence = EvidenceTracker()
        _merge_fallback_result(
            lead0,
            {"emails": ["fallback@second.com"], "phone_numbers": [], "contact_forms": [], "socials": []},
            evidence,
            [],
            [],
        )
        self.assertEqual(lead0["email"], "primary@first.com")


class SocialHostMatchingTests(unittest.TestCase):
    def test_recallmax_is_not_x_com(self):
        from bs4 import BeautifulSoup

        # Regression: substring matching made 'recallmax.com' look like the
        # platform 'x.com', causing pointless multi-second junk fetches.
        soup = BeautifulSoup(
            '<a href="https://usa2.recallmax.com/rsm/patient.html">book</a>',
            "html.parser",
        )
        self.assertEqual(SocialParser.discover_socials(soup), [])

        profile = SocialParser._normalize_social_url("https://usa2.recallmax.com/rsm/patient.html")
        self.assertIsNone(profile)

        profile = SocialParser._normalize_social_url("https://www.instagram.com/testbiz")
        self.assertIsNotNone(profile)
        self.assertEqual(profile.platform, "Instagram")

        profile = SocialParser._normalize_social_url("https://x.com/testbiz")
        self.assertIsNotNone(profile)
        self.assertEqual(profile.platform, "X/Twitter")

    def test_subdomain_matches_but_lookalikes_do_not(self):
        self.assertFalse(SocialParser._netloc_matches("usa2.recallmax.com", "x.com"))
        self.assertTrue(SocialParser._netloc_matches("www.facebook.com", "facebook.com"))
        self.assertTrue(SocialParser._netloc_matches("facebook.com", "facebook.com"))
        self.assertFalse(SocialParser._netloc_matches("notfacebook.com", "facebook.com"))


class InterruptedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patcher = patch.object(
            Config, "RESULTS_DIR", Path(self._tmp.name) / "jobs", create=True
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def test_running_snapshot_marked_interrupted(self):
        persistence.save_job({"task_id": "stale", "status": persistence.STATUS_RUNNING, "leads": []})
        persistence.save_job({"task_id": "done", "status": persistence.STATUS_COMPLETE, "leads": []})

        changed = persistence.mark_running_as_interrupted()

        self.assertEqual(changed, 1)
        self.assertEqual(persistence.load_job("stale")["status"], persistence.STATUS_INTERRUPTED)
        self.assertEqual(persistence.load_job("done")["status"], persistence.STATUS_COMPLETE)


if __name__ == "__main__":
    unittest.main()
