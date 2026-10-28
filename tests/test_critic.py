import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from methods.audit import critic  # noqa: E402


class CriticGatewayTests(unittest.TestCase):
    def setUp(self):
        self._original_gateway = critic._llm_gateway
        critic._llm_gateway = None

    def tearDown(self):
        critic._llm_gateway = self._original_gateway

    def test_gateway_is_initialized_lazily_once(self):
        sentinel = object()
        with patch.object(critic, "LLMGateway", return_value=sentinel) as factory:
            self.assertIs(critic._get_gateway(), sentinel)
            self.assertIs(critic._get_gateway(), sentinel)
            factory.assert_called_once()


class CriticNormalizationTests(unittest.TestCase):
    def test_hostile_output_is_normalized(self):
        result = critic.apply_qualification_rules(
            {
                "viability_score": "9999",
                "confidence_score": None,
                "design_verdict": "evil",
                "primary_defects": "not a list",
            }
        )
        self.assertEqual(result["viability_score"], 100)
        self.assertEqual(result["confidence_score"], 0)
        self.assertEqual(result["design_verdict"], "passable")
        self.assertEqual(result["primary_defects"], [])
        self.assertFalse(result["needs_redesign"])

    def test_evidence_backed_result_qualifies(self):
        result = critic.apply_qualification_rules(
            {
                "viability_score": 80,
                "confidence_score": 90,
                "design_verdict": "outdated",
                "primary_defects": ["No viewport meta tag", "Slow mobile load"],
            }
        )
        self.assertTrue(result["qualified"])
        self.assertTrue(result["needs_redesign"])
        self.assertEqual(len(result["primary_defects"]), 2)


if __name__ == "__main__":
    unittest.main()
