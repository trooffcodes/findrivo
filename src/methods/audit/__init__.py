from typing import Any, Dict
from urllib.parse import urljoin

from bs4 import BeautifulSoup
import requests

from core.logger import app_logger, track_performance
from .network import (
    normalize_target,
    check_ssl,
    fetch_page,
    check_links_concurrently,
    is_internal_link,
    _MAX_SAMPLE_LINKS,
    _REQUEST_HEADERS,
)

from .css_auditor import audit_css
from .ui_auditor import audit_ui_heuristics, build_design_skeleton
from .critic import qualify_redesign


@track_performance("audit_domain_vitals")
def audit_website(url: str, enable_llm_critic: bool = True) -> Dict[str, Any]:
    """Unified entrypoint running network, CSS, UI, and redesign qualification audits."""
    url, domain, port = normalize_target(url)
    app_logger.info("Audit started | target=%s | domain=%s", url, domain)

    audit_result: Dict[str, Any] = {
        "url": url,
        "is_live": False,
        "load_time_seconds": 0.0,
        "has_ssl": False,
        "ssl_details": {},
        "is_mobile_viewport": False,
        "broken_links_count": 0,
        "broken_links_details": [],
        "css_audit": {},
        "ui_heuristics": {},
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

    # 2. HTTP Request
    session = requests.Session()
    session.headers.update(_REQUEST_HEADERS)

    try:
        html, load_time, status_code, err = fetch_page(url, session)
        audit_result["load_time_seconds"] = load_time

        if err or status_code != 200 or not html:
            audit_result["issues_found"].append(err or f"Non-200 HTTP status code: {status_code}")
            return audit_result

        audit_result["is_live"] = True
        if load_time > 3.0:
            audit_result["issues_found"].append(f"Slow response time: {load_time}s")

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

        # 7. LLM Redesign Qualification (~150 tokens)
        if enable_llm_critic:
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