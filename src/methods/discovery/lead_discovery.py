"""
methods/discovery/lead_discovery.py
Playwright Google Maps discovery engine with stealth fingerprinting,
resource blocking, atomic progress checkpoints, and graceful dependency handling.
"""

import gc
import json
import os
import queue
import re
import threading
import time
import urllib.parse
from typing import Any, Dict, Generator, List, Optional, Union

from config import Config
from core.logger import app_logger
from . import antidetect
from . import stealth
from . import validator

_STATE_LOCK = threading.Lock()


def _empty_progress() -> dict:
    return {"processed_urls": [], "completed_queries": []}


def _load_progress(filepath: str) -> dict:
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {
                    "processed_urls": [
                        str(v) for v in data.get("processed_urls", []) if isinstance(v, str)
                    ],
                    "completed_queries": [
                        str(v) for v in data.get("completed_queries", []) if isinstance(v, str)
                    ],
                }
        except Exception as err:
            app_logger.warning("Ignoring unreadable progress file %s: %s", filepath, err)
    return _empty_progress()


def _acquire_file_lock(lock_path: str, timeout: float = 2.0) -> int:
    """Best-effort cross-process lock using atomic file creation."""
    deadline = time.time() + timeout
    while True:
        try:
            return os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > 30:
                    os.unlink(lock_path)
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                raise TimeoutError(f"Timed out waiting for progress lock {lock_path}")
            time.sleep(0.05)


def _save_progress_atomic(filepath: str, state: dict):
    """Merge and atomically persist progress under a process-local and file lock."""
    with _STATE_LOCK:
        lock_path = filepath + ".lock"
        lock_fd = None
        try:
            directory = os.path.dirname(filepath)
            if directory:
                os.makedirs(directory, exist_ok=True)
            lock_fd = _acquire_file_lock(lock_path)

            existing = _load_progress(filepath) if os.path.exists(filepath) else _empty_progress()
            merged = {
                "processed_urls": sorted(
                    set(existing["processed_urls"]) | set(state.get("processed_urls", []))
                ),
                "completed_queries": sorted(
                    set(existing["completed_queries"]) | set(state.get("completed_queries", []))
                ),
            }
            tmp = f"{filepath}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, filepath)
        except Exception as e:
            app_logger.warning("Unable to persist discovery progress: %s", e)
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
                try:
                    os.unlink(lock_path)
                except OSError:
                    pass


def _extract_place_fast(page, default_category: str) -> dict:
    """Fast DOM extraction with single-pass priority selectors."""
    try:
        page.wait_for_selector("h1, div[role='main']", timeout=4000)
    except Exception:
        pass

    # Business Name
    name = ""
    for sel in ["h1.DUwDvf", "h1.fontHeadlineLarge", "h1"]:
        el = page.query_selector(sel)
        if el and el.inner_text().strip():
            name = el.inner_text().strip()
            break
    if not name:
        try:
            name = page.title().split(" - ")[0].strip() or "Unknown Business"
        except Exception:
            name = "Unknown Business"

    # Category
    category = default_category
    el = page.query_selector("button[jsaction*='category'], button.Dkftq")
    if el and el.inner_text().strip():
        category = el.inner_text().strip()

    # Address
    address = ""
    el = page.query_selector("button[data-item-id='address'], button[aria-label*='Address:']")
    if el:
        txt = el.get_attribute("aria-label") or el.inner_text()
        address = txt.replace("Address:", "").replace("\ue0c8", "").strip()

    # Phone
    phone = ""
    el = page.query_selector("button[data-item-id^='phone:tel:'], button[aria-label*='Phone:']")
    if el:
        txt = el.get_attribute("aria-label") or el.inner_text()
        phone = txt.replace("Phone:", "").replace("tel:", "").replace("\ue0cd", "").strip()

    # Website
    website = ""
    el = page.query_selector("a[data-item-id='authority'], a[aria-label*='Website:']")
    if el:
        website = el.get_attribute("href") or ""

    # Rating & Reviews
    rating = ""
    el = page.query_selector("div.F7L825, span.ceNzR")
    if el:
        txt = el.get_attribute("aria-label") or el.inner_text()
        m = re.search(r"([\d\.]+)", txt)
        if m and float(m.group(1)) <= 5.0:
            rating = m.group(1)

    reviews = ""
    el = page.query_selector("button[aria-label*='reviews'], span[aria-label*='reviews']")
    if el:
        txt = el.get_attribute("aria-label") or el.inner_text()
        m = re.search(r"([\d,]+)", txt)
        if m:
            reviews = m.group(1).replace(",", "")

    return {
        "name": name,
        "category": category,
        "address": address,
        "phone": phone,
        "website": website,
        "rating": rating,
        "reviews": reviews,
    }


def _worker_thread_loop(
    worker_id: int,
    task_queue: queue.Queue,
    result_queue: queue.Queue,
    stop_event: threading.Event,
    state: dict,
    progress_file: str,
    max_leads: int,
    headless: bool,
    persist_progress: bool,
):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        app_logger.error("Playwright package not installed. Run: pip install playwright && playwright install chromium")
        return

    worker_tag = f"Worker-{worker_id}"
    pacing = antidetect.AdaptivePacingEngine(Config.RANDOM_DELAY_MIN, Config.RANDOM_DELAY_MAX)

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=headless, args=stealth.STEALTH_CHROMIUM_ARGS)
            except Exception as e:
                app_logger.error(
                    "Playwright Chromium launch failed: %s. Run 'playwright install chromium' to install binaries.",
                    e,
                )
                return

            def make_context():
                ctx = browser.new_context(
                    viewport=antidetect.get_random_viewport(),
                    user_agent=antidetect.get_random_user_agent(),
                    locale="en-US",
                )
                page = ctx.new_page()
                stealth.apply_stealth_and_speed(ctx, page, block_resources=Config.BLOCK_HEAVY_RESOURCES)
                return ctx, page

            context, page = make_context()
            scrape_count = 0

            try:
                while not stop_event.is_set():
                    try:
                        category, location = task_queue.get(timeout=1.0)
                    except queue.Empty:
                        break

                    # A task is either a (niche, location) pair or a complete
                    # planner query string passed as (query, "").
                    query = f"{category} in {location}" if location else category
                    query_key = f"{category}|{location}"
                    t_query = time.perf_counter()
                    app_logger.info("[%s] Scrape query started: '%s'", worker_tag, query)

                    search_url = f"https://www.google.com/maps/search/{urllib.parse.quote(query)}"
                    try:
                        page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
                        page.wait_for_timeout(1000)
                        app_logger.info(
                            "[%s] Maps search page loaded in %.1fs", worker_tag, time.perf_counter() - t_query
                        )
                    except Exception as e:
                        app_logger.warning(
                            "[%s] Navigation to Maps search failed after %.1fs: %s",
                            worker_tag, time.perf_counter() - t_query, e,
                        )
                        task_queue.task_done()
                        continue

                    # Dismiss consent modals
                    for btn in ["button:has-text('Accept all')", "button:has-text('I agree')"]:
                        try:
                            if page.is_visible(btn):
                                page.click(btn, timeout=600)
                                page.wait_for_timeout(300)
                        except Exception:
                            pass

                    # Feed link harvesting — bounded in both result count and
                    # wall time so a slow feed can never freeze the worker.
                    place_urls: List[str] = []
                    stagnant_count = 0
                    last_len = 0
                    deadline = time.perf_counter() + Config.DISCOVERY_SCROLL_SECONDS
                    end_reason = "max_leads"

                    # JS fallback: scroll the results feed container directly.
                    # Mouse wheel events only scroll whatever element is under
                    # the cursor, so without this (and without parking the
                    # cursor on the feed) the list never leaves the first
                    # viewport and harvesting stalls at ~20 results.
                    _SCROLL_FEED_JS = (
                        "() => { const f = document.querySelector('div[role=\"feed\"]');"
                        " if (f) { f.scrollTop = f.scrollHeight; return true; }"
                        " const w = document.scrollingElement;"
                        " if (w) { w.scrollTop = w.scrollHeight; return true; }"
                        " return false; }()"
                    )

                    while (
                        len(place_urls) < max_leads
                        and stagnant_count < Config.DISCOVERY_SCROLL_STAGNANT
                        and not stop_event.is_set()
                        and time.perf_counter() < deadline
                    ):
                        anchors = page.query_selector_all("a[href*='/maps/place/']")
                        for a in anchors:
                            href = a.get_attribute("href")
                            if href and href not in place_urls:
                                place_urls.append(href)
                                if len(place_urls) >= max_leads:
                                    break

                        # Park the cursor on the results feed so the wheel
                        # event scrolls the list, not the map or the header.
                        try:
                            feed = page.query_selector('div[role="feed"]')
                            if feed:
                                box = feed.bounding_box()
                                if box:
                                    page.mouse.move(
                                        box["x"] + box["width"] / 2,
                                        box["y"] + min(box["height"] / 2, 200),
                                    )
                        except Exception:
                            pass

                        # mouse.wheel instead of page.evaluate: a wheel event is
                        # fire-and-forget and cannot hang on a frozen renderer.
                        try:
                            page.mouse.wheel(0, 2500)
                        except Exception:
                            pass
                        # Belt-and-braces: force the feed container itself.
                        try:
                            page.evaluate(_SCROLL_FEED_JS)
                        except Exception:
                            pass
                        page.wait_for_timeout(700)

                        if len(place_urls) == last_len:
                            stagnant_count += 1
                            if stagnant_count >= Config.DISCOVERY_SCROLL_STAGNANT:
                                end_reason = "no_new_results"
                        else:
                            stagnant_count = 0
                            last_len = len(place_urls)

                        if time.perf_counter() >= deadline and end_reason == "max_leads":
                            end_reason = "time_budget"

                    if not place_urls:
                        app_logger.warning(
                            "[%s] No place links harvested for '%s' (feed did not render?)",
                            worker_tag, query,
                        )
                    else:
                        app_logger.info(
                            "[%s] Harvested %d place links in %.1fs (end=%s)",
                            worker_tag, len(place_urls), time.perf_counter() - t_query, end_reason,
                        )
                        result_queue.put({
                            "__progress__": True,
                            "message": f"Scraped Maps feed for '{query}' — {len(place_urls)} candidates found, extracting business data…",
                        })

                    # Extract places
                    for idx, place_url in enumerate(place_urls, start=1):
                        if stop_event.is_set():
                            break

                        with _STATE_LOCK:
                            if place_url in state["processed_urls"]:
                                continue

                        scrape_count += 1
                        time.sleep(pacing.get_delay())

                        if scrape_count % 40 == 0:
                            gc.collect()
                            try:
                                context.close()
                                context, page = make_context()
                            except Exception:
                                pass

                        t_place = time.perf_counter()
                        result_queue.put({
                            "__progress__": True,
                            "message": f"Reading Maps listing {idx}/{len(place_urls)}…",
                        })
                        try:
                            page.goto(place_url, wait_until="domcontentloaded", timeout=12000)
                            page.wait_for_timeout(600)
                            pacing.record_latency(time.perf_counter() - t_place)
                        except Exception as e:
                            app_logger.warning(
                                "[%s] Place %d/%d failed after %.1fs: %s",
                                worker_tag, idx, len(place_urls),
                                time.perf_counter() - t_place, e,
                            )
                            with _STATE_LOCK:
                                state["processed_urls"].append(place_url)
                            continue

                        extracted = _extract_place_fast(page, category)
                        raw_website = extracted["website"]
                        is_social = any(
                            s in raw_website.lower()
                            for s in ["instagram.com", "facebook.com", "linkedin.com", "twitter.com", "x.com"]
                        )
                        official_website = "" if is_social else raw_website

                        # Fast Enrichment
                        # NOTE: deliberately NOT calling enrichment.enrich_website_fast
                        # here. The pipeline stage re-fetches the website for the
                        # audit + deep extraction anyway; a second HTTP pass per
                        # listing (up to ~4 extra seconds, or ~12s with subpages)
                        # duplicated that work and delayed first results.
                        lead = {
                            "worker_id": worker_tag,
                            "name": extracted["name"],
                            "niche": category,
                            "category": extracted["category"],
                            "search_location": location,
                            "address": extracted["address"],
                            "phone": extracted["phone"],
                            "website": official_website,
                            "listed_website": raw_website,
                            "email": "",
                            "whatsapp": "",
                            "facebook": "",
                            "instagram": "",
                            "linkedin": "",
                            "twitter": "",
                            "contact_form": "",
                            "rating": extracted["rating"],
                            "reviews": extracted["reviews"],
                            "maps_link": place_url,
                            "website_status": "",
                            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                        }

                        validated = validator.process_raw_lead(lead)

                        with _STATE_LOCK:
                            state["processed_urls"].append(place_url)
                        if persist_progress:
                            _save_progress_atomic(progress_file, state)

                        app_logger.info(
                            "[%s] Place %d/%d extracted in %.1fs: %s (%s)",
                            worker_tag, idx, len(place_urls),
                            time.perf_counter() - t_place,
                            extracted["name"] or "Unknown",
                            "website" if official_website else "no official website",
                        )
                        result_queue.put(validated)

                    if not stop_event.is_set():
                        with _STATE_LOCK:
                            state["completed_queries"].append(query_key)
                        if persist_progress:
                            _save_progress_atomic(progress_file, state)
                    task_queue.task_done()

            finally:
                browser.close()

    except Exception as err:
        app_logger.error("[%s] Worker fatal error: %s", worker_tag, err)


def discover_businesses(
    niche: Union[List[str], str],
    location: Union[List[str], str],
    concurrency: int = Config.DISCOVERY_CONCURRENCY,
    max_leads_per_location: int = Config.MAX_LEADS_PER_LOCATION,
    headless: bool = Config.HEADLESS,
    progress_file: str = Config.PROGRESS_FILE,
    resume: bool = Config.RESUME_DISCOVERY,
    queries: Optional[List[str]] = None,
) -> Generator[Dict[str, Any], None, None]:
    """Concurrent Google Maps streaming discovery engine.

    Progress resume is opt-in. Without it, each run starts with a clean state so
    a stale shared progress file cannot silently suppress all future leads.

    `queries`, when provided, is a list of complete search strings (e.g. from
    the planner agent) and takes precedence over the niche x location cross
    product — one Maps feed per query instead of a single feed for the whole
    job.
    """
    state = _load_progress(progress_file) if resume else _empty_progress()
    completed = set(state.get("completed_queries", []))

    task_queue = queue.Queue()
    result_queue = queue.Queue()
    stop_event = threading.Event()

    total_tasks = 0
    if queries:
        for q in queries:
            q = str(q).strip()
            if q and f"{q}|" not in completed:
                task_queue.put((q, ""))
                total_tasks += 1
    else:
        niches = [niche] if isinstance(niche, str) else niche
        locations = [location] if isinstance(location, str) else location
        for cat in niches:
            for loc in locations:
                if f"{cat}|{loc}" not in completed:
                    task_queue.put((cat, loc))
                    total_tasks += 1

    if total_tasks == 0:
        app_logger.info("Target search queries have already been scraped in %s", progress_file)
        return

    workers_to_spawn = min(concurrency, total_tasks)
    threads: List[threading.Thread] = []

    for i in range(workers_to_spawn):
        t = threading.Thread(
            target=_worker_thread_loop,
            args=(
                i + 1,
                task_queue,
                result_queue,
                stop_event,
                state,
                progress_file,
                max_leads_per_location,
                headless,
                resume,
            ),
            daemon=True,
        )
        t.start()
        threads.append(t)

    app_logger.info("Discovery stream started | queries=%d | workers=%d", total_tasks, workers_to_spawn)
    yielded_leads = 0
    try:
        while any(t.is_alive() for t in threads) or not result_queue.empty():
            try:
                lead = result_queue.get(timeout=0.3)
                if not (isinstance(lead, dict) and lead.get("__progress__")):
                    yielded_leads += 1
                yield lead
                result_queue.task_done()
            except queue.Empty:
                continue
    finally:
        stop_event.set()
        for t in threads:
            t.join(timeout=2.0)
        app_logger.info("Discovery stream finished | raw_leads_yielded=%d", yielded_leads)