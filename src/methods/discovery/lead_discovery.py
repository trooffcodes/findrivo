"""
High-Speed Concurrent Google Maps Discovery Engine
"""

import os
import re
import json
import time
import gc
import queue
import threading
import urllib.parse
from typing import Generator, Dict, Any, List, Union

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from config import Config
from core.logger import app_logger, track_performance
import antidetect
import stealth
import validator
import enrichment

_STATE_LOCK = threading.Lock()


def _load_progress(filepath: str) -> dict:
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {"processed_urls": [], "completed_queries": []}


def _save_progress_atomic(filepath: str, state: dict):
    with _STATE_LOCK:
        tmp = filepath + ".tmp"
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            os.replace(tmp, filepath)
        except Exception as e:
            app_logger.error("State save error: %s", e)


def _extract_place_fast(page, default_category: str) -> dict:
    """Fast DOM extraction with prioritized single-pass selectors."""
    try:
        page.wait_for_selector("h1, div[role='main']", timeout=4500)
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
        address = txt.replace("Address:", "").replace('\ue0c8', '').strip()

    # Phone
    phone = ""
    el = page.query_selector("button[data-item-id^='phone:tel:'], button[aria-label*='Phone:']")
    if el:
        txt = el.get_attribute("aria-label") or el.inner_text()
        phone = txt.replace("Phone:", "").replace("tel:", "").replace('\ue0cd', '').strip()

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
        m = re.search(r'([\d\.]+)', txt)
        if m and float(m.group(1)) <= 5.0:
            rating = m.group(1)

    reviews = ""
    el = page.query_selector("button[aria-label*='reviews'], span[aria-label*='reviews']")
    if el:
        txt = el.get_attribute("aria-label") or el.inner_text()
        m = re.search(r'([\d,]+)', txt)
        if m:
            reviews = m.group(1).replace(',', '')

    return {
        "name": name,
        "category": category,
        "address": address,
        "phone": phone,
        "website": website,
        "rating": rating,
        "reviews": reviews
    }


def _worker_thread_loop(
    worker_id: int,
    task_queue: queue.Queue,
    result_queue: queue.Queue,
    stop_event: threading.Event,
    state: dict,
    progress_file: str,
    max_leads: int,
    headless: bool
):
    worker_tag = f"Worker-{worker_id}"
    app_logger.info("[%s] ⚡ Instance spawned and ready.", worker_tag)
    pacing = antidetect.AdaptivePacingEngine(Config.RANDOM_DELAY_MIN, Config.RANDOM_DELAY_MAX)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=stealth.STEALTH_CHROMIUM_ARGS
        )

        def make_context():
            ctx = browser.new_context(
                viewport=antidetect.get_random_viewport(),
                user_agent=antidetect.get_random_user_agent(),
                locale="en-US"
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

                query = f"{category} in {location}"
                query_key = f"{category}|{location}"
                app_logger.info("[%s] 🎯 Claimed Task: '%s'", worker_tag, query)

                search_url = f"https://www.google.com/maps/search/{urllib.parse.quote(query)}"
                t_start = time.perf_counter()

                try:
                    page.goto(search_url, wait_until="domcontentloaded", timeout=22000)
                    page.wait_for_timeout(1000)
                except Exception as e:
                    app_logger.warning("[%s] ❌ Search load failure: %s", worker_tag, e)
                    task_queue.task_done()
                    continue

                # Dismiss consent modals
                for btn in ["button:has-text('Accept all')", "button:has-text('I agree')"]:
                    try:
                        if page.is_visible(btn):
                            page.click(btn, timeout=800)
                            page.wait_for_timeout(400)
                    except Exception:
                        pass

                # Fast feed scrolling
                place_urls = []
                stagnant_count = 0
                last_len = 0

                app_logger.info("[%s] 📜 Harvesting feed links for '%s'...", worker_tag, query)
                while len(place_urls) < max_leads and stagnant_count < 3 and not stop_event.is_set():
                    anchors = page.query_selector_all("a[href*='/maps/place/']")
                    for a in anchors:
                        href = a.get_attribute("href")
                        if href and href not in place_urls:
                            place_urls.append(href)
                            if len(place_urls) >= max_leads:
                                break

                    # Accelerated scrolling jump
                    page.evaluate("""
                        const feed = document.querySelector("div[role='feed']");
                        if (feed) feed.scrollBy(0, 2500);
                        else window.scrollBy(0, 2500);
                    """)
                    page.wait_for_timeout(750)

                    if len(place_urls) == last_len:
                        stagnant_count += 1
                    else:
                        stagnant_count = 0
                        last_len = len(place_urls)

                app_logger.info("[%s] ✅ Collected %d targets in %.2fs. Processing individual places...",
                                worker_tag, len(place_urls), time.perf_counter() - t_start)

                # Process extracted items
                for idx, place_url in enumerate(place_urls, start=1):
                    if stop_event.is_set():
                        break

                    with _STATE_LOCK:
                        if place_url in state["processed_urls"]:
                            app_logger.debug("[%s] Skipping duplicate: %s", worker_tag, place_url[:60])
                            continue

                    scrape_count += 1
                    time.sleep(pacing.get_delay())

                    # Context recycling every 50 leads to keep memory minimal
                    if scrape_count % 50 == 0:
                        gc.collect()
                        try:
                            context.close()
                            context, page = make_context()
                            app_logger.debug("[%s] Browser context recycled (Count: %d)", worker_tag, scrape_count)
                        except Exception:
                            pass

                    t_place = time.perf_counter()
                    try:
                        page.goto(place_url, wait_until="domcontentloaded", timeout=14000)
                        page.wait_for_timeout(800)
                        pacing.record_latency(time.perf_counter() - t_place)
                    except Exception:
                        continue

                    # Extract DOM info
                    extracted = _extract_place_fast(page, category)
                    raw_website = extracted["website"]

                    # Social check
                    is_social = any(s in raw_website.lower() for s in ["instagram.com", "facebook.com", "linkedin.com", "twitter.com", "x.com"])
                    official_website = "" if is_social else raw_website

                    # Parallel Website Enrichment
                    enriched = enrichment.enrich_website_fast(official_website)

                    # Build output dict
                    lead = {
                        "worker_id": worker_tag,
                        "name": extracted["name"],
                        "niche": category,
                        "category": extracted["category"],
                        "search_location": location,
                        "address": extracted["address"],
                        "phone": extracted["phone"],
                        "website": official_website,
                        "email": enriched["email"],
                        "whatsapp": enriched["whatsapp"],
                        "facebook": enriched["facebook"],
                        "instagram": enriched["instagram"],
                        "linkedin": enriched["linkedin"],
                        "twitter": enriched["twitter"],
                        "contact_form": enriched["contact_form"],
                        "rating": extracted["rating"],
                        "reviews": extracted["reviews"],
                        "maps_link": place_url,
                        "website_status": enriched["website_status"],
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                    }

                    validated = validator.process_raw_lead(lead)

                    app_logger.info(
                        "[%s] 📍 [%d/%d] Found: '%s' | Phone: %s | Web: %s | Email: %s (%.2fs)",
                        worker_tag, idx, len(place_urls),
                        validated["name"],
                        validated["phone"] or "None",
                        "Yes" if validated["website"] else "No",
                        validated["email"] or "None",
                        time.perf_counter() - t_place
                    )

                    # Persist checkpoint atomically
                    with _STATE_LOCK:
                        state["processed_urls"].append(place_url)
                    _save_progress_atomic(progress_file, state)

                    # Stream to generator consumer
                    result_queue.put(validated)

                # Query complete
                with _STATE_LOCK:
                    state["completed_queries"].append(query_key)
                _save_progress_atomic(progress_file, state)

                app_logger.info("[%s] 🏁 Query '%s' finished in %.1fs", worker_tag, query, time.perf_counter() - t_start)
                task_queue.task_done()

        finally:
            browser.close()
            app_logger.info("[%s] Closed cleanly.", worker_tag)


@track_performance("discover_businesses_stream")
def discover_businesses(
    niche: Union[List[str], str],
    location: Union[List[str], str],
    concurrency: int = Config.CONCURRENCY,
    max_leads_per_location: int = Config.MAX_LEADS_PER_LOCATION,
    headless: bool = Config.HEADLESS,
    progress_file: str = Config.PROGRESS_FILE
) -> Generator[Dict[str, Any], None, None]:
    """
    Spawns concurrent worker threads and yields extracted business objects in real time.
    """
    niches = [niche] if isinstance(niche, str) else niche
    locations = [location] if isinstance(location, str) else location

    state = _load_progress(progress_file)
    completed = set(state.get("completed_queries", []))

    task_queue = queue.Queue()
    result_queue = queue.Queue()
    stop_event = threading.Event()

    total_tasks = 0
    for cat in niches:
        for loc in locations:
            if f"{cat}|{loc}" not in completed:
                task_queue.put((cat, loc))
                total_tasks += 1

    if total_tasks == 0:
        app_logger.info("All target search queries have already been scraped.")
        return

    workers_to_spawn = min(concurrency, total_tasks)
    app_logger.info("Launching %d simultaneous discovery executions...", workers_to_spawn)

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
                headless
            ),
            daemon=True
        )
        t.start()
        threads.append(t)

    # Stream results back to caller
    try:
        while any(t.is_alive() for t in threads) or not result_queue.empty():
            try:
                lead = result_queue.get(timeout=0.3)
                yield lead
                result_queue.task_done()
            except queue.Empty:
                continue
    except (KeyboardInterrupt, GeneratorExit):
        app_logger.warning("Interrupted by caller. Halting discovery threads...")
        stop_event.set()
    finally:
        stop_event.set()
        for t in threads:
            t.join(timeout=2.5)


# =====================================================================
# US TEST HARNESS
# =====================================================================
if __name__ == "__main__":
    # Test queries targeting major US locations
    test_niches = ["Dental Clinic", "Coffee Roaster"]
    test_locations = ["Austin, TX", "Miami, FL"]

    print("=" * 70)
    print("STARTING 2 SIMULTANEOUS EXECUTIONS (US LOCATIONS)")
    print("=" * 70)

    for business in discover_businesses(
        niche=test_niches,
        location=test_locations,
        concurrency=2,
        max_leads_per_location=4,
        headless=True
    ):
        print(
            f"⚡ [{business['worker_id']}] Yielded: {business['name']} | "
            f"Phone: {business['phone']} | "
            f"Email: {business['email'] or 'N/A'} | "
            f"Website: {business['website'] or 'N/A'}"
        )