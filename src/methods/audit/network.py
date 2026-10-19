import concurrent.futures
import socket
import ssl
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from core.logger import app_logger, perf_logger, track_performance

_DEFAULT_TIMEOUT = 6.0
_LINK_CHECK_TIMEOUT = 3.0
_MAX_SAMPLE_LINKS = 5
_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def normalize_target(raw_url: str) -> Tuple[str, str, int]:
    url = raw_url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)
    hostname = parsed.hostname or parsed.netloc.split(":")[0]
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return url, hostname, port


@track_performance("ssl_handshake")
def check_ssl(hostname: str, port: int = 443) -> Dict[str, Any]:
    ssl_result: Dict[str, Any] = {
        "valid": False,
        "protocol": None,
        "days_remaining": None,
        "error": None,
    }

    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((hostname, port), timeout=3.0) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as secure_sock:
                cert = secure_sock.getpeercert()
                ssl_result["valid"] = True
                ssl_result["protocol"] = secure_sock.version()

                if cert and "notAfter" in cert:
                    expire_epoch = ssl.cert_time_to_seconds(cert["notAfter"])
                    days_left = int((expire_epoch - time.time()) / 86400)
                    ssl_result["days_remaining"] = days_left

        app_logger.debug("SSL verified | host=%s | days_left=%s", hostname, ssl_result["days_remaining"])
    except Exception as err:
        ssl_result["error"] = str(err)
        app_logger.warning("SSL error | host=%s | err=%s", hostname, err)

    return ssl_result


def fetch_page(url: str, session: requests.Session) -> Tuple[Optional[str], float, int, Optional[str]]:
    t_start = time.perf_counter()
    try:
        res = session.get(url, timeout=_DEFAULT_TIMEOUT, allow_redirects=True)
        load_time = round(time.perf_counter() - t_start, 3)
        perf_logger.info("Completed | page_fetch | status=%d | duration=%.4fs", res.status_code, load_time)
        return res.text, load_time, res.status_code, None
    except requests.RequestException as err:
        load_time = round(time.perf_counter() - t_start, 3)
        app_logger.error("HTTP fetch failed | url=%s | err=%s", url, err)
        return None, load_time, 0, str(err)


def is_internal_link(target_url: str, base_host: str) -> bool:
    parsed = urlparse(target_url)
    if not parsed.hostname:
        return False
    return (
        parsed.hostname == base_host
        or parsed.hostname.endswith(f".{base_host}")
        or base_host.endswith(f".{parsed.hostname}")
    )


def _validate_link(session: requests.Session, target: str) -> Tuple[str, bool, Optional[int]]:
    try:
        res = session.head(target, timeout=_LINK_CHECK_TIMEOUT, allow_redirects=True)
        if res.status_code in (403, 405):
            res = session.get(target, timeout=_LINK_CHECK_TIMEOUT, stream=True, allow_redirects=True)
        return target, res.status_code < 400, res.status_code
    except requests.RequestException:
        return target, False, None


@track_performance("internal_link_check")
def check_links_concurrently(session: requests.Session, links: List[str]) -> Tuple[int, List[Dict[str, Any]]]:
    broken_count = 0
    failures = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(links))) as executor:
        future_map = {executor.submit(_validate_link, session, link): link for link in links}
        for future in concurrent.futures.as_completed(future_map):
            target, is_healthy, status_code = future.result()
            if not is_healthy:
                broken_count += 1
                failures.append({"url": target, "status_code": status_code})

    return broken_count, failures