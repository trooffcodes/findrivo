"""Link-classification checks used by the audit layer.

Run with: PYTHONPATH=src python -m unittest src.tests.dork_specific
This intentionally performs no network work.
"""

import unittest

from methods.audit.network import is_internal_link, normalize_target


class LinkTests(unittest.TestCase):
    def test_internal_link_matching(self):
        self.assertTrue(is_internal_link("https://example.com/about", "example.com"))
        self.assertTrue(is_internal_link("https://www.example.com/about", "example.com"))
        self.assertFalse(is_internal_link("https://notexample.com/about", "example.com"))
        self.assertFalse(is_internal_link("https://example.com.evil.test/", "example.com"))

    def test_normalize_target(self):
        url, host, port = normalize_target("example.com")
        self.assertEqual(url, "https://example.com")
        self.assertEqual(host, "example.com")
        self.assertEqual(port, 443)

    def test_normalize_target_rejects_malformed_input(self):
        for value in ("", "   ", "http://example.com:99999/"):
            with self.assertRaises(ValueError):
                normalize_target(value)


if __name__ == "__main__":
    unittest.main()
