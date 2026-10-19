import concurrent.futures
import json
import re
import socket
import ssl
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from core.llm_client import LLMGateway
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

# Shared gateway instance
_llm_gateway: Optional[LLMGateway] = None


def _get_gateway() -> LLMGateway:
    global _llm_gateway
    if _llm_gateway is None:
        _llm_gateway = LLMGateway()
    return _llm_gateway


def _normalize_target(raw_url: str) -> tuple[str, str, int]:
    url = raw_url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)
    hostname = parsed.hostname or parsed.netloc.split(":")[0]
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return url, hostname, port


@track_performance("ssl_handshake")
def _check_ssl(hostname: str, port: int = 443) -> Dict[str, Any]:
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


def _is_internal_link(target_url: str, base_host: str) -> bool:
    parsed = urlparse(target_url)
    if not parsed.hostname:
        return False
    return (
        parsed.hostname == base_host
        or parsed.hostname.endswith(f".{base_host}")
        or base_host.endswith(f".{parsed.hostname}")
    )


def _validate_link(session: requests.Session, target: str) -> tuple[str, bool, Optional[int]]:
    try:
        res = session.head(target, timeout=_LINK_CHECK_TIMEOUT, allow_redirects=True)
        if res.status_code in (403, 405):
            res = session.get(target, timeout=_LINK_CHECK_TIMEOUT, stream=True, allow_redirects=True)
        return target, res.status_code < 400, res.status_code
    except requests.RequestException:
        return target, False, None


def _check_links_concurrently(session: requests.Session, links: List[str]) -> tuple[int, List[Dict[str, Any]]]:
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


# ==============================================================================
# UI AUDIT SUITE (Zero-Token Heuristics + Token-Efficient LLM)
# ==============================================================================

@track_performance("ui_heuristics_audit")
def _audit_ui_heuristics(soup: BeautifulSoup) -> Dict[str, Any]:
    """Evaluates UI/Accessibility structure deterministically with BeautifulSoup (0 tokens)."""
    findings = []

    # 1. Heading Hierarchy
    h1_tags = soup.find_all("h1")
    if not h1_tags:
        findings.append("Missing primary <h1> heading for page structure")
    elif len(h1_tags) > 2:
        findings.append(f"Multiple <h1> headings detected ({len(h1_tags)} found), diluting visual hierarchy")

    # 2. Image Accessibility (Alt tags)
    images = soup.find_all("img")
    missing_alt = [img for img in images if not img.get("alt") or not img.get("alt").strip()]
    if missing_alt:
        findings.append(f"{len(missing_alt)} of {len(images)} images are missing descriptive alt text")

    # 3. Form Input Accessibility
    inputs = soup.find_all("input", attrs={"type": lambda t: t not in ("hidden", "submit", "button")})
    unlabeled_inputs = 0
    for inp in inputs:
        inp_id = inp.get("id")
        has_label = bool(inp_id and soup.find("label", attrs={"for": inp_id}))
        has_aria = bool(inp.get("aria-label") or inp.get("placeholder"))
        if not (has_label or has_aria):
            unlabeled_inputs += 1
    if unlabeled_inputs > 0:
        findings.append(f"{unlabeled_inputs} form inputs lack accessible labels or placeholders")

    # 4. Interactive Call-To-Action (CTA) Detection
    buttons = soup.find_all(["button", "a"])
    cta_patterns = re.compile(r"(start|get started|sign up|subscribe|book|try|contact|buy|demo|login)", re.I)
    has_primary_cta = any(cta_patterns.search(b.get_text()) for b in buttons)
    if not has_primary_cta:
        findings.append("No prominent Call-to-Action (CTA) found in interactive elements")

    # 5. Visual Icon / Favicon Check
    has_favicon = bool(soup.find("link", rel=lambda r: r and "icon" in r))
    if not has_favicon:
        findings.append("Missing browser tab favicon")

    return {
        "h1_count": len(h1_tags),
        "total_images": len(images),
        "images_missing_alt": len(missing_alt),
        "has_primary_cta": has_primary_cta,
        "has_favicon": has_favicon,
        "issues": findings,
    }


def _extract_ui_skeleton(soup: BeautifulSoup) -> str:
    """Condenses a massive DOM into an ultra-compact ~150 token UI skeleton for LLM audit."""
    title = (soup.title.string.strip() if soup.title and soup.title.string else "Untitled")[:80]

    # Main headlines
    h1 = " | ".join(h.get_text(strip=True) for h in soup.find_all("h1")[:2])[:120]
    h2 = " | ".join(h.get_text(strip=True) for h in soup.find_all("h2")[:3])[:150]

    # Navigation menu links
    nav = soup.find("nav")
    nav_links = []
    if nav:
        nav_links = [a.get_text(strip=True) for a in nav.find_all("a") if a.get_text(strip=True)][:5]
    nav_summary = ", ".join(nav_links) if nav_links else "None detected"

    # Prominent button texts
    buttons = [
        btn.get_text(strip=True)
        for btn in soup.find_all(["button", "a"], class_=re.compile(r"btn|cta|primary|action", re.I))
        if btn.get_text(strip=True)
    ][:4]
    btn_summary = ", ".join(buttons) if buttons else "None detected"

    # Hero paragraph
    hero_p = ""
    first_p = soup.find("p")
    if first_p and first_p.get_text(strip=True):
        hero_p = first_p.get_text(strip=True)[:150]

    return (
        f"Page Title: {title}\n"
        f"H1: {h1 or 'None'}\n"
        f"H2 Subheadings: {h2 or 'None'}\n"
        f"Navigation Items: {nav_summary}\n"
        f"CTAs/Buttons: {btn_summary}\n"
        f"Hero Copy: {hero_p or 'None'}"
    )


@track_performance("llm_ux_audit")
def _audit_ui_with_llm(skeleton: str) -> Dict[str, Any]:
    """Queries LLM with token-efficient prompt for qualitative UI/UX critique."""
    app_logger.info("Executing LLM UI/UX critique...")

    system_prompt = (
        "You are an expert UI/UX auditor. Review the provided webpage UI skeleton. "
        "Evaluate: 1) Value proposition clarity, 2) CTA visibility/intent, 3) Cognitive load. "
        "Return STRICT JSON only matching this format: "
        '{"ux_score": <1-10>, "clarity": "<brief comment>", "friction_points": ["<point 1>", "<point 2>"]}'
    )

    user_prompt = f"Analyze this UI skeleton:\n{skeleton}"

    try:
        gateway = _get_gateway()
        raw_response = gateway.call_groq_with_fallback(
            prompt=user_prompt,
            system_prompt=system_prompt,
        )

        clean_json = raw_response.strip()
        if "```json" in clean_json:
            clean_json = clean_json.split("```json")[1].split("```")[0].strip()
        elif "```" in clean_json:
            clean_json = clean_json.split("```")[1].split("```")[0].strip()

        # Regex fallback for embedded JSON object
        match = re.search(r"\{.*\}", clean_json, re.DOTALL)
        if match:
            clean_json = match.group(0)

        return json.loads(clean_json)

    except Exception as err:
        app_logger.warning("LLM UI critique failed | err=%s", err)
        return {
            "ux_score": None,
            "clarity": "Automated UX evaluation skipped due to API error.",
            "friction_points": [str(err)],
        }


# ==============================================================================
# MAIN ENTRYPOINT
# ==============================================================================

@track_performance("audit_domain_vitals")
def audit_website(url: str, enable_llm_ui: bool = True) -> dict:
    url, domain, port = _normalize_target(url)
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
        "ui_heuristics": {},
        "llm_ux_analysis": {},
        "issues_found": [],
    }

    # 1. SSL Audit
    ssl_info = _check_ssl(domain, port)
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

    # 2. HTTP Request & Latency
    session = requests.Session()
    session.headers.update(_REQUEST_HEADERS)

    try:
        t_req_start = time.perf_counter()
        res = session.get(url, timeout=_DEFAULT_TIMEOUT, allow_redirects=True)
        load_time = round(time.perf_counter() - t_req_start, 3)
        audit_result["load_time_seconds"] = load_time

        perf_logger.info(
            "Completed | operation=%s | status=%d | duration=%.4fs",
            "page_fetch",
            res.status_code,
            load_time,
        )

        if res.status_code == 200:
            audit_result["is_live"] = True
        else:
            audit_result["issues_found"].append(f"Returns non-200 status code: {res.status_code}")
            return audit_result

        if load_time > 3.0:
            audit_result["issues_found"].append(f"Slow load speed: {load_time}s")

        # 3. HTML Parsing & Viewport
        with track_performance("html_parsing"):
            soup = BeautifulSoup(res.text, "html.parser")

        viewport = soup.find("meta", attrs={"name": "viewport"})
        viewport_content = viewport.get("content", "").lower() if viewport else ""
        if "width=device-width" in viewport_content:
            audit_result["is_mobile_viewport"] = True
        else:
            audit_result["issues_found"].append(
                "Not configured for mobile devices (Missing width=device-width in viewport)"
            )

        # 4. Structural UI Audit (0 tokens)
        ui_heuristics = _audit_ui_heuristics(soup)
        audit_result["ui_heuristics"] = ui_heuristics
        audit_result["issues_found"].extend(ui_heuristics["issues"])

        # 5. Token-Efficient LLM UX Audit (~150 input tokens)
        if enable_llm_ui:
            skeleton = _extract_ui_skeleton(soup)
            ux_critique = _audit_ui_with_llm(skeleton)
            audit_result["llm_ux_analysis"] = ux_critique
            if ux_critique.get("friction_points"):
                for point in ux_critique["friction_points"]:
                    audit_result["issues_found"].append(f"UX Friction: {point}")

        # 6. Sampled Internal Link Audit
        candidate_links = []
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
                continue

            resolved_target = urljoin(url, href)
            if resolved_target.startswith(("http://", "https://")) and _is_internal_link(resolved_target, domain):
                if resolved_target not in candidate_links:
                    candidate_links.append(resolved_target)
                if len(candidate_links) >= _MAX_SAMPLE_LINKS:
                    break

        with track_performance("internal_link_check"):
            broken_count, failures = _check_links_concurrently(session, candidate_links)
            audit_result["broken_links_count"] = broken_count
            audit_result["broken_links_details"] = failures

        if broken_count > 0:
            audit_result["issues_found"].append(
                f"{broken_count} of {len(candidate_links)} sampled internal links returned errors"
            )

    except requests.exceptions.RequestException as err:
        app_logger.error("HTTP request failed | url=%s | err=%s", url, err)
        audit_result["issues_found"].append(f"Connection error: {str(err)}")
    finally:
        session.close()

    app_logger.info(
        "Audit completed | target=%s | live=%s | issues_count=%d",
        url,
        audit_result["is_live"],
        len(audit_result["issues_found"]),
    )
    return audit_result


if __name__ == "__main__":
    result = audit_website("https://unlost-nine.vercel.app")
    import pprint
    pprint.pprint(result)