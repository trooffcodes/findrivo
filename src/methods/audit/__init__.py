from typing import Any, Dict, List
from urllib.parse import urljoin

from bs4 import BeautifulSoup
import requests

from core.logger import app_logger, track_performance
from .network import (
    normalize_target,
    check_ssl,
    fetch_page,  # noqa: F401  (re-exported for compatibility)
    fetch_page_samples,
    check_links_concurrently,
    is_internal_link,
    _MAX_SAMPLE_LINKS,
    _REQUEST_HEADERS,
)

from .css_auditor import audit_css
from .ui_auditor import audit_ui_heuristics, build_design_skeleton
from .critic import qualify_redesign

# Page-weight sampling bounds: HEAD-check at most this many images, each with
# this timeout. Keeps the added audit cost to well under a second in practice.
_PAGE_WEIGHT_MAX_IMAGES = 8
_PAGE_WEIGHT_TIMEOUT = 3.0


def _measure_page_weight(html: str, soup: Any, session: Any, base_url: str) -> Dict[str, Any]:
    """Bounded page-weight estimate: HTML size + HEAD checks on a few images.

    Deliberately lightweight: no rendering, no full asset crawl — just the
    homepage HTML size and Content-Length of up to 8 sampled images fetched
    concurrently with short timeouts.
    """
    import concurrent.futures

    html_bytes = len(html.encode("utf-8", "ignore"))

    img_urls: List[str] = []
    for img in soup.find_all("img", src=True):
        src = str(img.get("src") or "").strip()
        if not src or src.startswith(("data:", "javascript:")):
            continue
        resolved = urljoin(base_url, src)
        if resolved.startswith("http") and resolved not in img_urls:
            img_urls.append(resolved)
        if len(img_urls) >= _PAGE_WEIGHT_MAX_IMAGES:
            break

    def _head_size(u: str) -> int:
        try:
            resp = session.head(u, timeout=_PAGE_WEIGHT_TIMEOUT, allow_redirects=True)
            cl = resp.headers.get("Content-Length", "")
            return int(cl) if cl.isdigit() else 0
        except Exception:
            return 0

    image_bytes = 0
    measured = 0
    if img_urls:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            sizes = list(pool.map(_head_size, img_urls))
        image_bytes = sum(sizes)
        measured = sum(1 for s in sizes if s > 0)

    total_bytes = html_bytes + image_bytes
    return {
        "html_bytes": html_bytes,
        "image_count": len(soup.find_all("img")),
        "sampled_images": len(img_urls),
        "measured_images": measured,
        "image_bytes": image_bytes,
        "total_bytes": total_bytes,
        "total_mb": round(total_bytes / (1024 * 1024), 2),
        "method": "html_size_plus_head_sampled_images",
    }


@track_performance("audit_domain_vitals")
def audit_website(url: str, enable_llm_critic: bool = True) -> Dict[str, Any]:
    """Unified entrypoint running network, CSS, UI, and redesign qualification audits."""
    url, domain, port = normalize_target(url)
    app_logger.info("Audit started | target=%s | domain=%s", url, domain)

    audit_result: Dict[str, Any] = {
        "url": url,
        "is_live": False,
        "load_time_seconds": 0.0,
        "response_time": {},
        "has_ssl": False,
        "ssl_details": {},
        "is_mobile_viewport": False,
        "broken_links_count": 0,
        "broken_links_details": [],
        "css_audit": {},
        "ui_heuristics": {},
        "page_weight": {},
        "redesign_qualification": {},
        "issues_found": [],
    }

    # 1. SSL Audit
    ssl_info = check_ssl(domain, port)
    audit_result["has_ssl"] = ssl_info["valid"]
    audit_result["ssl_details"] = ssl_info

    if not ssl_info["valid"]:
        audit_result["issues_found"].append(
            ssl_info["error"] or "Missing or invalid SSL certificate (Insecure HTTP)"
        )
    elif ssl_info["days_remaining"] is not None and ssl_info["days_remaining"] <= 14:
        audit_result["issues_found"].append(
            f"SSL certificate expires soon ({ssl_info['days_remaining']} days remaining)"
        )

    # 2. HTTP Request(s) — up to two GETs to damp single-sample noise.
    session = requests.Session()
    session.headers.update(_REQUEST_HEADERS)

    try:
        html, load_time, status_code, err, response_meta = fetch_page_samples(url, session)
        audit_result["load_time_seconds"] = load_time
        audit_result["response_time"] = {
            "seconds": load_time,
            "samples": response_meta.get("samples", 0),
            "basis": response_meta.get("basis", "http_get_duration"),
            "inconclusive": response_meta.get("inconclusive", True),
            "reliable": response_meta.get("reliable", False),
            "method": "client-side HTTP GET duration (DNS, TLS and network included; "
            "not browser page-load time)",
        }

        if err or status_code != 200 or not html:
            audit_result["issues_found"].append(err or f"Non-200 HTTP status code: {status_code}")
            return audit_result

        audit_result["is_live"] = True
        if load_time > 3.0:
            if response_meta.get("reliable"):
                audit_result["issues_found"].append(
                    f"Slow median response time across {response_meta['samples']} samples: {load_time}s"
                )
            else:
                audit_result["issues_found"].append(
                    f"Response time was slow in a single sample ({load_time}s); inconclusive"
                )

        # 3. DOM & Viewport
        soup = BeautifulSoup(html, "html.parser")
        viewport = soup.find("meta", attrs={"name": "viewport"})
        viewport_content = viewport.get("content", "").lower() if viewport else ""
        if "width=device-width" in viewport_content:
            audit_result["is_mobile_viewport"] = True
        else:
            audit_result["issues_found"].append(
                "Not mobile responsive (Missing width=device-width in viewport meta)"
            )

        # 3b. Lightweight page-weight sampling (HTML size + a few image HEADs).
        try:
            audit_result["page_weight"] = _measure_page_weight(html, soup, session, url)
        except Exception as pw_err:
            app_logger.debug("Page-weight sampling failed for %s: %s", url, pw_err)

        # 4. Actual CSS Audit (Rules, media queries, flexbox/grid, fixed widths)
        css_data = audit_css(soup=soup, base_url=url, session=session)
        audit_result["css_audit"] = css_data
        audit_result["issues_found"].extend(css_data["issues"])

        # 5. UI & Hierarchy Audit
        ui_data = audit_ui_heuristics(soup)
        audit_result["ui_heuristics"] = ui_data
        audit_result["issues_found"].extend(ui_data["issues"])

        # 6. Sampled Internal Links
        candidate_links = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
                continue
            resolved = urljoin(url, href)
            if resolved.startswith(("http://", "https://")) and is_internal_link(resolved, domain):
                if resolved not in candidate_links:
                    candidate_links.append(resolved)
                if len(candidate_links) >= _MAX_SAMPLE_LINKS:
                    break

        broken_count, failures = check_links_concurrently(session, candidate_links)
        audit_result["broken_links_count"] = broken_count
        audit_result["broken_links_details"] = failures
        if broken_count > 0:
            audit_result["issues_found"].append(
                f"{broken_count} of {len(candidate_links)} sampled internal links returned errors"
            )

        # 7. LLM Redesign Qualification (~150 tokens).
        # Skipped for dead sites: a critic cannot meaningfully judge a website
        # that did not respond, and the analysis call costs real latency every
        # lead despite adding nothing (observed defects already cover the audit).
        if enable_llm_critic and audit_result["is_live"]:
            skeleton = build_design_skeleton(
                soup=soup,
                css_data=css_data,
                ui_data=ui_data,
                load_time=load_time,
                has_ssl=audit_result["has_ssl"],
                is_mobile_viewport=audit_result["is_mobile_viewport"],
            )
            qualification = qualify_redesign(skeleton, audit_result["issues_found"])
            audit_result["redesign_qualification"] = qualification

    finally:
        session.close()

    app_logger.info(
        "Audit completed | target=%s | live=%s | issues=%d | qualified=%s",
        url,
        audit_result["is_live"],
        len(audit_result["issues_found"]),
        audit_result.get("redesign_qualification", {}).get("qualified", False),
    )
    return audit_result


__all__ = ["audit_website"]