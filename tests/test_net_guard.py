import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.net_guard import UnsafeURLError, safe_request, validate_public_url  # noqa: E402
from methods.audit.network import check_ssl  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code=200, location=None):
        self.status_code = status_code
        self.headers = {"Location": location} if location else {}
        self.is_redirect = bool(location)
        self.closed = False

    def close(self):
        self.closed = True


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requested = []
        self.allow_redirects_values = []

    def request(self, method, url, **kwargs):
        self.requested.append(url)
        self.allow_redirects_values.append(kwargs.get("allow_redirects"))
        return self.responses.pop(0)


def _addrinfo(address: str):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (address, 0))]


class PublicURLValidationTests(unittest.TestCase):
    def test_blocks_loopback(self):
        with patch("core.net_guard.socket.getaddrinfo", return_value=_addrinfo("127.0.0.1")):
            with self.assertRaises(UnsafeURLError):
                validate_public_url("http://example.com")

    def test_blocks_private_ranges(self):
        for address in ("10.0.0.5", "192.168.1.10", "169.254.169.254", "::1", "fc00::1"):
            with patch(
                "core.net_guard.socket.getaddrinfo", return_value=_addrinfo(address)
            ):
                with self.assertRaises(UnsafeURLError):
                    validate_public_url("https://example.com")

    def test_allows_public_address(self):
        with patch("core.net_guard.socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            self.assertEqual(validate_public_url("https://example.com/path"), "https://example.com/path")

    def test_rejects_non_http_schemes_and_credentials(self):
        for url in ("file:///etc/passwd", "ftp://example.com", "https://user:pass@example.com"):
            with self.assertRaises(UnsafeURLError):
                validate_public_url(url)

    def test_rejects_localhost_names(self):
        for url in ("http://localhost/", "http://localhost.localdomain/"):
            with self.assertRaises(UnsafeURLError):
                validate_public_url(url)

    def test_malformed_urls_raise_unsafe_url_error(self):
        for url in ("http://example.com:99999/", "http://[::1/", "http://"):
            with self.assertRaises(UnsafeURLError):
                validate_public_url(url)


class RedirectSafetyTests(unittest.TestCase):
    def test_redirect_targets_are_validated_and_redirects_disabled_in_client(self):
        first = _FakeResponse(302, "https://b.example/next")
        second = _FakeResponse(200)
        session = _FakeSession([first, second])
        with patch("core.net_guard.validate_public_url", side_effect=lambda url: url):
            response = safe_request(session, "GET", "https://a.example/")
        self.assertIs(response, second)
        self.assertEqual(
            session.requested, ["https://a.example/", "https://b.example/next"]
        )
        # requests must never follow redirects on its own; we validate each hop.
        self.assertEqual(session.allow_redirects_values, [False, False])
        self.assertTrue(first.closed)

    def test_unsafe_redirect_target_is_rejected(self):
        first = _FakeResponse(302, "http://169.254.169.254/latest/meta-data/")
        session = _FakeSession([first])

        def validator(url):
            if "169.254.169.254" in url:
                raise UnsafeURLError("link-local")
            return url

        with patch("core.net_guard.validate_public_url", side_effect=validator):
            with self.assertRaises(UnsafeURLError):
                safe_request(session, "GET", "https://a.example/")

    def test_redirect_loop_is_bounded(self):
        responses = [_FakeResponse(302, "https://a.example/") for _ in range(10)]
        session = _FakeSession(responses)
        with patch("core.net_guard.validate_public_url", side_effect=lambda url: url):
            with self.assertRaises(UnsafeURLError):
                safe_request(session, "GET", "https://a.example/", max_redirects=3)


class SSLTargetGuardTests(unittest.TestCase):
    def test_private_tls_target_is_blocked_before_socket_connect(self):
        with patch(
            "methods.audit.network.validate_public_url",
            side_effect=UnsafeURLError("private address"),
        ), patch("methods.audit.network.socket.create_connection") as connect:
            result = check_ssl("internal.example")
        connect.assert_not_called()
        self.assertFalse(result["valid"])
        self.assertIn("Blocked unsafe host", result["error"])


if __name__ == "__main__":
    unittest.main()
