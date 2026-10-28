import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from methods.discovery.validator import (  # noqa: E402
    normalize_phone,
    normalize_url,
    parse_rating_val,
    parse_reviews_count,
    process_raw_lead,
    validate_email_address,
)


class DiscoveryValidatorTests(unittest.TestCase):
    def test_email_validation_rejects_tracker_noise(self):
        self.assertEqual(validate_email_address("real@business.com"), "real@business.com")
        for value in ("a@example.com", "test@business.com", "logo@2x.png", "not-an-email"):
            self.assertEqual(validate_email_address(value), "")

    def test_url_normalization(self):
        self.assertEqual(normalize_url("business.com"), "https://business.com")
        self.assertEqual(normalize_url("n/a"), "")

    def test_phone_normalization(self):
        self.assertEqual(normalize_phone("+1 (512) 555-0100"), "+1 (512) 555-0100")
        self.assertEqual(normalize_phone("5125550100"), "(512) 555-0100")
        self.assertEqual(normalize_phone(""), "")

    def test_reviews_and_ratings(self):
        self.assertEqual(parse_reviews_count("1.2K"), 1200)
        self.assertEqual(parse_reviews_count("2,345"), 2345)
        self.assertEqual(parse_rating_val("4.8"), 4.8)
        self.assertEqual(parse_rating_val("9"), 0.0)

    def test_process_raw_lead(self):
        cleaned = process_raw_lead(
            {
                "phone": "5125550100",
                "website": "example.net",
                "email": "owner@example.net",
                "rating": "4.5",
                "reviews": "10",
            }
        )
        self.assertEqual(cleaned["phone"], "(512) 555-0100")
        self.assertEqual(cleaned["website"], "https://example.net")
        self.assertEqual(cleaned["email"], "owner@example.net")
        self.assertEqual(cleaned["rating"], 4.5)


if __name__ == "__main__":
    unittest.main()
