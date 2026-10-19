"""
extraction/fetcher.py
Lightweight HTTP network layer using requests with pooled sessions,
rotating browser-realistic headers, and safe timeout handling.
"""

import random
from typing import Optional, Tuple
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.logger import app_logger, perf_logger, track_performance

USER_AGENTS = [
    # Chrome on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    # Chrome on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    # Firefox on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    # Safari on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]


class LightweightFetcher:
    """Robust, pool-managed requests session with retries and browser impersonation."""

    def __init__(self, timeout: int = 10, max_retries: int = 2):
        self.timeout = timeout
        self.session = requests.Session()

        retries = Retry(
            total=max_retries,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(
            max_retries=retries, pool_connections=20, pool_maxsize=20
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _get_headers(self) -> dict:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "DNT": "1",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        }

    @track_performance("fetch_page")
    def fetch(self, url: str) -> Tuple[Optional[str], int, str]:
        """
        Retrieves page HTML.
        Returns:
            Tuple[Optional[str], int, str]: (html_content, status_code, final_url)
        """
        try:
            response = self.session.get(
                url,
                headers=self._get_headers(),
                timeout=self.timeout,
                allow_redirects=True,
                verify=True,
            )
            # Inspect encoding and fall back gracefully
            if response.encoding is None or response.encoding == "ISO-8859-1":
                response.encoding = response.apparent_encoding

            perf_logger.info(
                "Fetched URL: %s | Status: %d | Size: %d bytes",
                url,
                response.status_code,
                len(response.content),
            )
            if response.status_code < 400:
                return response.text, response.status_code, response.url
            
            app_logger.warning("HTTP %d for URL: %s", response.status_code, url)
            return None, response.status_code, response.url

        except requests.exceptions.SSLError:
            app_logger.warning("SSL Error on %s. Retrying without SSL verify...", url)
            try:
                response = self.session.get(
                    url,
                    headers=self._get_headers(),
                    timeout=self.timeout,
                    allow_redirects=True,
                    verify=False,
                )
                return response.text, response.status_code, response.url
            except Exception as e:
                app_logger.error("Failed non-SSL fetch for %s: %s", url, e)
                return None, 0, url

        except requests.exceptions.RequestException as e:
            app_logger.warning("Network failure requesting %s: %s", url, e)
            return None, 0, url

    def close(self):
        self.session.close()