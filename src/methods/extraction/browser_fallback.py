"""
methods/extraction/browser_fallback.py
Bounded Playwright fallback extraction.

Used only when a lead is promising but the lightweight HTTP path could not
recover important information (see `should_browser_fallback`). Rendering is
handled by a small pool of dedicated worker threads (each owning one Chromium
instance) fed from a shared task queue, so several leads can be rendered
simultaneously and callers may submit early and collect the result later
instead of blocking. Hosts that fail are memoized
and not retried for a cooldown, so broken sites are never hammered repeatedly.
"""

import queue
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from config import Config
from core.logger import app_logger
from .parsers.contact_parser import ContactParser
from .parsers.meta_parser import MetaParser
from .parsers.social_parser import SocialParser


class _Task:
    __slots__ = ("url", "result", "done")

    def __init__(self, url: str):
        self.url = url
        self.result: Optional[Dict[str, Any]] = None
        self.done = threading.Event()

    def wait_result(self, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Block until the task finishes (or the timeout elapses) and return the result."""
        self.done.wait(timeout=timeout)
        return self.result


class _FallbackWorker(threading.Thread):
    """Owns one Playwright browser used for fallback extraction.

    Multiple workers share a single task queue so N leads can be rendered
    simultaneously instead of serializing behind one Chromium instance.
    """

    _worker_seq = 0

    def __init__(self, timeout: float, tasks: "queue.Queue[Optional[_Task]]"):
        _FallbackWorker._worker_seq += 1
        super().__init__(daemon=True, name=f"findrivo-browser-fallback-{_FallbackWorker._worker_seq}")
        self._tasks = tasks
        self._timeout = timeout
        self._stopping = threading.Event()

    def stop(self) -> None:
        self._stopping.set()

    def run(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            app_logger.error(
                "Playwright is not installed; browser fallback disabled. "
                "Run: pip install playwright && playwright install chromium"
            )
            while not self._stopping.is_set():
                try:
                    self._tasks.get(timeout=0.5).done.set()
                except queue.Empty:
                    continue
            return

        playwright = sync_playwright().start()
        browser = None
        try:
            from methods.discovery import stealth  # local import avoids a cycle at import time

            browser = playwright.chromium.launch(
                headless=Config.HEADLESS,
                args=stealth.STEALTH_CHROMIUM_ARGS,
            )
        except Exception as err:
            app_logger.error("Browser fallback launch failed: %s", err)
            self._fail_all_remaining("browser launch failed")
            return

        try:
            while not self._stopping.is_set():
                try:
                    task = self._tasks.get(timeout=1.0)
                except queue.Empty:
                    continue
                if task is None:
                    break
                context = None
                page = None
                try:
                    context = browser.new_context(locale="en-US")
                    page = context.new_page()
                    self._run_task(page, task)
                except Exception as err:
                    app_logger.warning(
                        "Browser fallback failed for %s: %s", task.url, err
                    )
                    _remember_failure(task.url)
                finally:
                    task.done.set()
                    try:
                        page.close() if page else None
                    except Exception:
                        pass
                    try:
                        context.close() if context else None
                    except Exception:
                        pass
        finally:
            try:
                if browser:
                    browser.close()
            except Exception:
                pass
            try:
                playwright.stop()
            except Exception:
                pass

    def _run_task(self, page: Any, task: _Task) -> None:
        # Hard deadline for the whole task: the first goto must leave room for
        # the contact-page pass, and the contact-page pass is skipped entirely
        # when the budget is already spent.
        deadline = time.monotonic() + self._timeout
        page.goto(task.url, wait_until="domcontentloaded", timeout=int(self._timeout * 1000))
        page.wait_for_timeout(800)
        # Scroll to trigger lazy content, but do not wait indefinitely.
        try:
            page.evaluate("window.scrollBy(0, document.body.scrollHeight * 0.6)")
        except Exception:
            pass
        page.wait_for_timeout(400)

        html = page.content()
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        url = page.url if page.url.startswith("http") else task.url

        from urllib.parse import urljoin

        meta = MetaParser.parse(soup)
        emails = set(meta.get("schema_emails", []))
        phones = set(meta.get("schema_phones", []))
        addresses = set(meta.get("schema_addresses", []))
        forms = set()

        emails.update(ContactParser.extract_emails(soup, html))
        phones.update(ContactParser.extract_phone_numbers(soup, html))
        addresses.update(ContactParser.extract_addresses(soup))
        forms.update(ContactParser.extract_contact_forms(soup, url))
        socials = SocialParser.discover_socials(soup, meta.get("schema_same_as", []))

        # Click-through to the first contact-looking link when still nothing.
        if not emails and not phones:
            contact_href = None
            for a in soup.find_all("a", href=True):
                text = (a.get_text(" ") or "").lower()
                if "contact" in text or "/contact" in str(a["href"]).lower():
                    contact_href = urljoin(url, a["href"])
                    break
            if contact_href:
                remaining = deadline - time.monotonic()
                if remaining < 2.0:
                    return  # Budget spent; ship what the first page yielded.
                try:
                    page.goto(
                        contact_href,
                        wait_until="domcontentloaded",
                        timeout=int(min(self._timeout, remaining) * 1000),
                    )
                    page.wait_for_timeout(600)
                    sub_html = page.content()
                    sub_soup = BeautifulSoup(sub_html, "html.parser")
                    sub_meta = MetaParser.parse(sub_soup)
                    emails.update(sub_meta.get("schema_emails", []))
                    phones.update(sub_meta.get("schema_phones", []))
                    emails.update(ContactParser.extract_emails(sub_soup, sub_html))
                    phones.update(ContactParser.extract_phone_numbers(sub_soup, sub_html))
                    addresses.update(ContactParser.extract_addresses(sub_soup))
                    forms.update(ContactParser.extract_contact_forms(sub_soup, page.url))
                except Exception:
                    pass  # Secondary page failing is not fatal to the task.

        task.result = {
            "emails": sorted(e for e in emails if e),
            "phone_numbers": sorted(p for p in phones if p),
            "addresses": sorted(a for a in addresses if a),
            "contact_forms": sorted(f for f in forms if f),
            "description": meta.get("description") or "",
            # Serialize models: callers merge plain dicts; a stray Pydantic
            # object previously crashed the whole lead record.
            "socials": [
                s.model_dump() if hasattr(s, "model_dump") else dict(s)
                for s in socials[:6]
            ],
            "pages_crawled": [url],
            "source": "official_website_browser",
        }

    def _fail_all_remaining(self, reason: str) -> None:
        while True:
            try:
                task = self._tasks.get(timeout=0.2)
            except queue.Empty:
                return
            if task is None:
                self._tasks.put_nowait(None)
                return
            app_logger.warning("Browser fallback dropped %s: %s", task.url, reason)
            _remember_failure(task.url)
            task.done.set()


# ------------------------------ pool management ------------------------------

_workers: List[_FallbackWorker] = []
_task_queue: "queue.Queue[Optional[_Task]]" = queue.Queue()
_worker_lock = threading.Lock()
_FAILURE_CACHE: Dict[str, float] = {}
_FAILURE_LOCK = threading.Lock()
_PER_JOB_COUNTER = threading.local()


def _remember_failure(url: str) -> None:
    host = urlparse(url).netloc if url else ""
    with _FAILURE_LOCK:
        _FAILURE_CACHE[host] = time.monotonic()


def _host_recently_failed(url: str) -> bool:
    host = urlparse(url).netloc if url else ""
    with _FAILURE_LOCK:
        failed_at = _FAILURE_CACHE.get(host)
    if failed_at is None:
        return False
    return (time.monotonic() - failed_at) < Config.BROWSER_FALLBACK_RETRY_COOLDOWN


def _ensure_workers() -> int:
    """Start the configured number of fallback workers (idempotent)."""
    with _worker_lock:
        _workers[:] = [w for w in _workers if w.is_alive()]
        while len(_workers) < Config.BROWSER_FALLBACK_WORKERS:
            worker = _FallbackWorker(Config.BROWSER_FALLBACK_TIMEOUT, _task_queue)
            worker.start()
            _workers.append(worker)
        return len(_workers)


def submit_extract(url: str) -> Optional[_Task]:
    """Queue a rendered extraction WITHOUT blocking the caller.

    Returns a handle whose `wait_result(timeout)` yields the result (or None).
    This lets a lead keep working (personalization, pitch) while its browser
    render runs in parallel, and lets several leads render simultaneously.
    Enforces the per-job budget so browser work stays bounded.
    """
    if not url or _host_recently_failed(url):
        return None
    current = getattr(_PER_JOB_COUNTER, "count", 0)
    if current >= Config.BROWSER_FALLBACK_MAX_PER_JOB:
        return None
    if _ensure_workers() == 0:
        return None

    task = _Task(url)
    try:
        _task_queue.put(task, timeout=0.5)
    except queue.Full:
        # Pool is saturated (e.g. shutdown): drop instead of piling up.
        task.done.set()
        return task
    _PER_JOB_COUNTER.count = current + 1
    app_logger.info(
        "Browser fallback queued for %s (queue depth=%d, workers=%d)",
        url, _task_queue.qsize(), len(_workers),
    )
    return task


def extract_rendered(url: str) -> Optional[Dict[str, Any]]:
    """Render `url` with Playwright and extract business data (blocking).

    Returns None on failure, timeout, unsupported host, or when the pool is
    unavailable. Failures are isolated: this never raises into the caller.
    """
    task = submit_extract(url)
    if task is None:
        return None
    task.done.wait(timeout=Config.BROWSER_FALLBACK_TIMEOUT * 2 + 5)
    return task.result


def reset_per_job_budget() -> None:
    """Reset the per-thread job budget counter (called once per pipeline job)."""
    _PER_JOB_COUNTER.count = 0


def try_extract_within_budget(url: str) -> Optional[Dict[str, Any]]:
    """Blocking extract; the per-job cap now lives in `submit_extract`."""
    return extract_rendered(url)


def shutdown_pool(timeout: float = 5.0) -> None:
    """Close all shared fallback browsers during app shutdown."""
    with _worker_lock:
        workers, _workers[:] = list(_workers), []
    for worker in workers:
        worker.stop()
    for _ in workers:
        try:
            _task_queue.put_nowait(None)
        except queue.Full:
            pass
    for worker in workers:
        worker.join(timeout=timeout)
