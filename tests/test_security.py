import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from config import Config  # noqa: E402
import app as app_module  # noqa: E402


class DownloadPathTests(unittest.TestCase):
    def test_safe_export_path_rejects_traversal_and_non_csv(self):
        self.assertIsNone(app_module._safe_export_path("../secret.csv"))
        self.assertIsNone(app_module._safe_export_path("..\\secret.csv"))
        self.assertIsNone(app_module._safe_export_path("nested/secret.csv"))
        self.assertIsNone(app_module._safe_export_path("notes.txt"))
        self.assertIsNone(app_module._safe_export_path(""))

    def test_safe_export_path_accepts_plain_csv(self):
        resolved = app_module._safe_export_path("leads_1234.csv")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.parent, Config.EXPORT_DIR.resolve())

    def test_download_endpoint_rejects_traversal(self):
        with patch.object(Config, "API_TOKEN", "test-token"), patch.object(
            Config, "ALLOW_UNAUTHENTICATED", False
        ):
            client = app_module.app.test_client()
            response = client.get(
                "/api/download/..%2F..%2Fsecret.csv",
                headers={"X-API-Key": "test-token"},
            )
            self.assertIn(response.status_code, (400, 404))

    def test_download_endpoint_serves_export_file(self):
        Config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(dir=Config.EXPORT_DIR, suffix=".csv")
        os.close(fd)
        try:
            with patch.object(Config, "API_TOKEN", "test-token"), patch.object(
                Config, "ALLOW_UNAUTHENTICATED", False
            ):
                client = app_module.app.test_client()
                response = client.get(
                    f"/api/download/{Path(path).name}",
                    headers={"X-API-Key": "test-token"},
                )
                self.assertEqual(response.status_code, 200)
                response.close()
        finally:
            os.unlink(path)

    def test_download_accepts_scoped_capability_token_not_master_query(self):
        Config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(dir=Config.EXPORT_DIR, suffix=".csv")
        os.close(fd)
        filename = Path(path).name
        app_module.ACTIVE_STREAMS["cap-test"] = {
            "queue": None,
            "created": 0,
            "csv_filename": filename,
            "token": "capability-token",
        }
        try:
            with patch.object(Config, "API_TOKEN", "master-token"), patch.object(
                Config, "ALLOW_UNAUTHENTICATED", False
            ):
                client = app_module.app.test_client()
                response = client.get(f"/api/download/{filename}?token=capability-token")
                self.assertEqual(response.status_code, 200)
                response.close()
                # The master secret must never be accepted from a URL.
                self.assertEqual(
                    client.get(f"/api/download/{filename}?api_key=master-token").status_code,
                    401,
                )
        finally:
            app_module.ACTIVE_STREAMS.pop("cap-test", None)
            os.unlink(path)


class AuthenticationTests(unittest.TestCase):
    def test_start_requires_token_when_configured(self):
        with patch.object(Config, "API_TOKEN", "test-token"), patch.object(
            Config, "ALLOW_UNAUTHENTICATED", False
        ):
            client = app_module.app.test_client()
            response = client.post("/api/pipeline/start", json={"query": "Dental in Austin"})
            self.assertEqual(response.status_code, 401)

    def test_start_accepts_valid_token_and_validates_count(self):
        with patch.object(Config, "API_TOKEN", "test-token"), patch.object(
            Config, "ALLOW_UNAUTHENTICATED", False
        ):
            client = app_module.app.test_client()
            response = client.post(
                "/api/pipeline/start",
                json={"query": "Dental in Austin", "target_count": 0},
                headers={"X-API-Key": "test-token"},
            )
            self.assertEqual(response.status_code, 400)

    def test_stream_rejects_invalid_task(self):
        with patch.object(Config, "API_TOKEN", "test-token"), patch.object(
            Config, "ALLOW_UNAUTHENTICATED", False
        ):
            client = app_module.app.test_client()
            response = client.get("/api/pipeline/stream/not-a-task")
            self.assertEqual(response.status_code, 404)


class InputAndRateLimitTests(unittest.TestCase):
    def test_target_count_rejects_non_integer_and_out_of_range(self):
        for value in ("abc", 0, Config.MAX_TARGET_COUNT + 1):
            with self.assertRaises(ValueError):
                app_module._parse_target_count(value)
        self.assertEqual(app_module._parse_target_count("5"), 5)

    def test_rate_limit_enforced_per_remote_address(self):
        with app_module.app.test_request_context("/", environ_base={"REMOTE_ADDR": "203.0.113.9"}):
            with patch.object(Config, "RATE_LIMIT_REQUESTS", 1), patch.object(
                Config, "RATE_LIMIT_WINDOW_SECONDS", 60
            ):
                app_module._RATE_BUCKETS.clear()
                self.assertFalse(app_module._rate_limit_exceeded())
                self.assertTrue(app_module._rate_limit_exceeded())
                app_module._RATE_BUCKETS.clear()


class FrontendSafetyTests(unittest.TestCase):
    def test_dashboard_does_not_interpolate_lead_fields_into_innerhtml(self):
        template = app_module.UI_TEMPLATE
        self.assertNotIn("row.innerHTML", template)
        self.assertIn("textContent", template)
        self.assertIn("businessName.textContent", template)


class EndToEndStreamTests(unittest.TestCase):
    def test_authenticated_pipeline_streams_lead_and_done_event(self):
        def fake_execute(**kwargs):
            yield {
                "name": "Test Business",
                "email": "owner@test.example",
                "is_live": True,
                "has_ssl": True,
                "needs_redesign": True,
                "viability_score": 80,
                "design_verdict": "outdated",
                "audit_defects": ["slow"],
                "ai_cold_pitch_hook": "hook",
            }

        with patch.object(Config, "API_TOKEN", "test-token"), patch.object(
            Config, "ALLOW_UNAUTHENTICATED", False
        ), patch.object(app_module.pipeline, "execute", new=fake_execute), tempfile.TemporaryDirectory() as tmp:
            with patch.object(Config, "RESULTS_DIR", Path(tmp) / "jobs", create=True):
                client = app_module.app.test_client()
                response = client.post(
                    "/api/pipeline/start",
                    json={"query": "Dental in Austin", "target_count": 1},
                    headers={"X-API-Key": "test-token"},
                )
                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertIn("token", payload)

                stream = client.get(
                    f"/api/pipeline/stream/{payload['task_id']}?token={payload['token']}"
                )
                body = stream.get_data(as_text=True)
                self.assertEqual(stream.status_code, 200)
                self.assertIn("Test Business", body)
                self.assertIn('"__event__": "done"', body)


class SecurityHeaderTests(unittest.TestCase):
    def test_html_gets_defense_in_depth_headers(self):
        client = app_module.app.test_client()
        response = client.get("/")
        self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(response.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(response.headers.get("Referrer-Policy"), "no-referrer")
        self.assertIn("frame-ancestors 'none'", response.headers.get("Content-Security-Policy", ""))
        response.close()

    def test_api_responses_are_not_cached(self):
        client = app_module.app.test_client()
        response = client.get("/api/health")
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        response.close()


class ProductionConfigTests(unittest.TestCase):
    def test_production_rejects_debug_and_missing_auth(self):
        with patch.object(Config, "IS_PRODUCTION", True), patch.object(
            Config, "DEBUG", True
        ), patch.object(Config, "API_TOKEN", ""), patch.object(
            Config, "ALLOW_UNAUTHENTICATED", True
        ):
            with self.assertRaises(RuntimeError):
                app_module.validate_runtime_config()


if __name__ == "__main__":
    unittest.main()
