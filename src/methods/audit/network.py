"""
methods/audit/network.py
Robust HTTP vitals, SSL certificate handshake, and concurrent link checking.
"""

import concurrent.futures
import socket
import ssl
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
import requests

from config import Config
from core.logger import app_logger, perf_logger, track_performance
from core.net_guard import UnsafeURLError, safe_request, validate_public_url

_DEFAULT_TIMEOUT = float(Config.REQUEST_TIMEOUT)
_LINK_CHECK_TIMEOUT = float(Config.LINK_CHECK_TIMEOUT)
_MAX_SAMPLE_LINKS = 5
_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def normalize_target(raw_url: str) -> Tuple[str, str, int]:
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ValueError("Empty website URL")

    url = raw_url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as err:
        raise ValueError(f"Malformed website URL: {err}") from err

    if not hostname:
        raise ValueError("Website URL has no hostname")
    return url, hostname, port


@track_performance("ssl_handshake")
def check_ssl(hostname: str, port: int = 443) -> Dict[str, Any]:
    """Validates SSL certificate days remaining with defensive timeout handling."""
    ssl_result: Dict[str, Any] = {
        "valid": False,
        "protocol": None,
        "days_remaining": None,
        "error": None,
    }

    if not hostname:
        ssl_result["error"] = "Empty hostname provided."
        return ssl_result

    # A raw TLS handshake is still an outbound connection: apply the same SSRF
    # policy used for HTTP fetches before opening a socket.
    try:
        host_for_url = f"[{hostname}]" if ":" in hostname else hostname
        validate_public_url(f"https://{host_for_url}:{port}")
    except UnsafeURLError as err:
        ssl_result["error"] = f"Blocked unsafe host: {err}"
        app_logger.debug("SSL target blocked | host=%s | err=%s", hostname, err)
        return ssl_result

    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((hostname, port), timeout=4.5) as sock:
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
        app_logger.debug("SSL handshake notice | host=%s | err=%s", hostname, err)

    return ssl_result


def fetch_page(url: str, session: requests.Session) -> Tuple[Optional[str], float, int, Optional[str]]:
    """Fetches HTML content without disabling TLS certificate validation.

    Kept for compatibility; new audit code should prefer `fetch_page_samples`
    because it provides an honestly-labelled response-time summary.
    """
    result = fetch_page_samples(url, session, max_samples=1)
    html, load_time, status_code, err, _meta = result
    return html, load_time, status_code, err


def fetch_page_samples(
    url: str,
    session: requests.Session,
    max_samples: int = 2,
) -> Tuple[Optional[str], float, int, Optional[str], Dict[str, Any]]:
    """Fetch HTML and summarize HTTP response duration across samples.

    Semantics made explicit here (this is deliberately NOT a page-load audit):

      * Every timing is a single GET request/response duration measured on the
        client, including DNS lookup, TLS handshake, and network transfer.
        It is not browser rendering time and not "the page's true speed".
      * Up to `max_samples` (max 2) GET requests are made, and only when the
        first one completes successfully; sites already failing are never
        re-tested just to obtain a second timing sample.
      * With two valid samples the summary is the median, which damps one-off
        network jitter; with one sample it is that single measurement.
      * `meta` labels how the number was produced so callers and the UI can
        present it honestly:
            samples       - number of successful measurements
            basis         - short description of the measurement semantics
            inconclusive  - True when no valid sample exists
            reliable      - True only when >= 2 successful samples
    """
    meta: Dict[str, Any] = {
        "samples": 0,
        "basis": "http_get_duration",
        "inconclusive": True,
        "reliable": False,
        "durations": [],
    }

    html = None
    status_code = 0
    err = None

    for attempt in range(max(1, min(max_samples, 2))):
        t_start = time.perf_counter()
        try:
            res = safe_request(session, "GET", url, timeout=_DEFAULT_TIMEOUT)
            duration = round(time.perf_counter() - t_start, 3)
            perf_logger.info(
                "Completed | page_fetch | status=%d | duration=%.4fs",
                res.status_code,
                duration,
            )
            status_code = res.status_code
            if res.status_code == 200 and res.text:
                html = res.text
                meta["durations"].append(duration)
                meta["samples"] += 1
                # A second sample is only useful when the first succeeded;
                # never re-test sites that already failed to respond.
                if attempt + 1 >= max_samples:
                    break
            else:
                if not html:
                    err = _describe_http_status(res.status_code, url)
                break
        except UnsafeURLError as e:
            err = f"Blocked unsafe URL: {e}"
            break
        except requests.RequestException as e:
            err = str(e)
            break  # A failed connection is not retried here.

    durations = sorted(meta["durations"])
    if not durations:
        load_time = 0.0
    elif len(durations) == 1:
        load_time = durations[0]
        meta["basis"] = "single_http_get_duration"
    else:
        mid = len(durations) // 2
        load_time = round((durations[mid - 1] + durations[mid]) / 2, 3) if len(durations) % 2 == 0 else durations[mid]
        meta["basis"] = f"median_of_{len(durations)}_http_get_durations"

    if html is not None:
        meta["inconclusive"] = False
        meta["reliable"] = meta["samples"] >= 2
    meta["durations"] = durations
    return html, load_time, status_code, err, meta


def _describe_http_status(status_code: int, url: str) -> Optional[str]:
    if status_code == 200:
        return None
    return f"Non-200 HTTP status code for {url}: {status_code}"


def is_internal_link(target_url: str, base_host: str) -> bool:
    try:
        parsed = urlparse(target_url)
    except ValueError:
        return False
    if not parsed.hostname or not base_host:
        return False
    host = parsed.hostname.lower().rstrip(".")
    base = base_host.lower().rstrip(".")
    return host == base or host.endswith(f".{base}")


def _validate_link(session: requests.Session, target: str) -> Tuple[str, bool, Optional[int]]:
    try:
        res = safe_request(session, "HEAD", target, timeout=_LINK_CHECK_TIMEOUT)
        if res.status_code in (403, 405):
            res = safe_request(session, "GET", target, timeout=_LINK_CHECK_TIMEOUT, stream=True)
            res.close()
        return target, res.status_code < 400, res.status_code
    except (requests.RequestException, UnsafeURLError):
        return target, False, None


@track_performance("internal_link_check")
def check_links_concurrently(session: requests.Session, links: List[str]) -> Tuple[int, List[Dict[str, Any]]]:
    broken_count = 0
    failures = []

    if not links:
        return 0, []

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(links), 5)) as executor:
        future_map = {executor.submit(_validate_link, session, link): link for link in links}
        for future in concurrent.futures.as_completed(future_map):
            target, is_healthy, status_code = future.result()
            if not is_healthy:
                broken_count += 1
                failures.append({"url": target, "status_code": status_code})

    return broken_count, failures