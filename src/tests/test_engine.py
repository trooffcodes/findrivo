"""Smoke checks for the current discovery entry points.

Run with: PYTHONPATH=src python -m unittest src.tests.engine_test
This intentionally performs no network or browser work.
"""

import unittest

from methods.discovery.lead_discovery import discover_businesses
from methods.discovery.validator import process_raw_lead


class DiscoverySmokeTests(unittest.TestCase):
    def test_discover_businesses_is_importable(self):
        self.assertTrue(callable(discover_businesses))

    def test_raw_lead_validation(self):
        lead = process_raw_lead(
            {
                "phone": "5125550100",
                "website": "business.example",
                "email": "owner@business.example",
                "rating": "4.5",
                "reviews": "12",
            }
        )
        self.assertEqual(lead["phone"], "(512) 555-0100")
        self.assertEqual(lead["website"], "https://business.example")


if __name__ == "__main__":
    unittest.main()
