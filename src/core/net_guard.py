"""
core/net_guard.py
SSRF protection and redirect-safe HTTP requests.

All outbound requests to user-supplied or scraped URLs must pass through this
module so that loopback, private, link-local, and otherwise non-public targets
cannot be reached by the server.
"""

import ipaddress
import socket
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from core.logger import app_logger

_ALLOWED_SCHEMES = {"http", "https"}
_MAX_REDIRECTS = 5


class UnsafeURLError(ValueError):
    """Raised when a URL resolves to a non-public or otherwise unsafe target."""


def _is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return False
    if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False
    return ip.is_global


def validate_public_url(url: str) -> str:
    """Validate scheme, hostname, and every resolved IP for a URL.

    DNS resolution is performed here because requests would otherwise resolve
    the hostname again later. This reduces (but does not eliminate) DNS
    rebinding risk; deployments should additionally use egress firewalls.
    """
    if not isinstance(url, str) or not url.strip():
        raise UnsafeURLError("Empty URL")

    try:
        parsed = urlparse(url.strip())
        hostname = (parsed.hostname or "").strip("[]").lower()
        port = parsed.port
    except ValueError as err:
        raise UnsafeURLError(f"Malformed URL: {err}") from err

    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(f"Unsupported URL scheme: {parsed.scheme!r}")
    if not hostname:
        raise UnsafeURLError("URL has no hostname")
    if parsed.username or parsed.password:
        raise UnsafeURLError("URLs containing embedded credentials are not allowed")

    if hostname in {"localhost", "localhost.localdomain"}:
        raise UnsafeURLError("Localhost URLs are not allowed")

    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as err:
        raise UnsafeURLError(f"Unable to resolve hostname {hostname!r}: {err}") from err

    addresses = {info[4][0] for info in infos if info and info[4]}
    if not addresses:
        raise UnsafeURLError(f"Hostname {hostname!r} resolved to no addresses")

    for address in addresses:
        if not _is_public_ip(address):
            raise UnsafeURLError(
                f"Hostname {hostname!r} resolves to non-public address {address!r}"
            )

    return parsed.geturl()


def safe_request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    max_redirects: int = _MAX_REDIRECTS,
    **kwargs: Any,
) -> requests.Response:
    """Perform an HTTP request while validating every redirect target.

    `allow_redirects` is controlled by this function. Callers must not pass it.
    """
    if kwargs.pop("allow_redirects", False):
        app_logger.debug("safe_request ignores caller allow_redirects; using guarded redirects")

    current_url = url
    for _ in range(max_redirects + 1):
        current_url = validate_public_url(current_url)
        response = session.request(method, current_url, allow_redirects=False, **kwargs)

        if response.is_redirect and response.headers.get("Location"):
            next_url = urljoin(current_url, response.headers["Location"])
            response.close()
            current_url = next_url
            continue

        return response

    raise UnsafeURLError(f"Too many redirects while fetching {url!r}")
