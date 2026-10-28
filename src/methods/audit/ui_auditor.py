
import re
from datetime import timezone, timedelta
from typing import Any, Dict

from bs4 import BeautifulSoup

_IST = timezone(timedelta(hours=5, minutes=30))

_CTA_PATTERN = re.compile(
    r"\b("
    r"get started|start now|sign up|register|book|contact|"
    r"buy|purchase|demo|try free|free trial|request a quote|"
    r"get a quote|subscribe|apply now|download|upload|"
    r"schedule|talk to us|let's talk|login|log in|sign in"
    r")\b",
    re.IGNORECASE,
)


def _element_text(element) -> str:
    """Extract concise visible text from a DOM element."""
    return element.get_text(" ", strip=True)[:120]


def _accessible_name(element) -> str:
    """Return common HTML sources for an element's accessible name."""
    return (
        element.get("aria-label")
        or element.get("title")
        or element.get("alt")
        or _element_text(element)
        or ""
    ).strip()


def audit_ui_heuristics(soup: BeautifulSoup) -> Dict[str, Any]:
    """Collect structural, CTA, accessibility, and carousel evidence."""
    issues = []

    h1_tags = soup.find_all("h1")
    h2_tags = soup.find_all("h2")

    if not h1_tags:
        issues.append("No h1 heading detected in the extracted HTML")

    images = soup.find_all("img")
    missing_alt = [
        image for image in images
        if image.get("alt") is None or not image.get("alt", "").strip()
    ]

    if missing_alt:
        issues.append(
            f"{len(missing_alt)} of {len(images)} images have no alt attribute "
            "or an empty alt value"
        )

    # Include buttons, links, and submit controls. A detector miss is not
    # proof that the rendered page has no CTA.
    interactive = soup.find_all(["a", "button", "input"])
    cta_candidates = []

    for element in interactive:
        input_type = (element.get("type") or "").lower()
        if element.name == "input" and input_type not in {
            "button", "submit", "image"
        }:
            continue

        name = _accessible_name(element)
        value = (element.get("value") or "").strip()
        candidate_text = name or value

        if candidate_text and _CTA_PATTERN.search(candidate_text):
            cta_candidates.append({
                "tag": element.name,
                "text": candidate_text[:100],
                "href": element.get("href"),
                "type": element.get("type"),
            })

    has_cta = bool(cta_candidates)
    if not has_cta:
        issues.append(
            "No CTA detected by the HTML text heuristic; verify rendered UI"
        )

    has_favicon = bool(
        soup.find(
            "link",
            rel=lambda value: value and any(
                "icon" in item.lower()
                for item in (
                    value if isinstance(value, list) else [value]
                )
            ),
        )
    )

    if not has_favicon:
        issues.append("No favicon link detected in the extracted HTML")

    # Copyright year is weak context, never a redesign defect by itself.
    copyright_year = None
    copyright_match = re.search(
        r"(?:©|&copy;|copyright)\s*"
        r"(?:[12]\d{3}\s*[-–—]\s*)?([12]\d{3})",
        soup.get_text(" ", strip=True),
        re.IGNORECASE,
    )

    if copyright_match:
        try:
            copyright_year = int(copyright_match.group(1))
        except ValueError:
            pass

    # Inspect basic form-label coverage without claiming full accessibility.
    inputs = soup.find_all(["input", "select", "textarea"])
    unlabeled_controls = []

    for element in inputs:
        input_type = (element.get("type") or "").lower()
        if element.name == "input" and input_type in {
            "hidden", "submit", "button", "reset", "image"
        }:
            continue

        element_id = element.get("id")
        has_label = bool(
            element.get("aria-label")
            or element.get("aria-labelledby")
            or element.find_parent("label")
            or (
                element_id
                and soup.find("label", attrs={"for": element_id})
            )
        )

        if not has_label:
            unlabeled_controls.append({
                "tag": element.name,
                "type": input_type or None,
                "name": element.get("name"),
                "id": element_id,
                "placeholder": element.get("placeholder"),
            })

    if unlabeled_controls:
        issues.append(
            f"{len(unlabeled_controls)} form controls have no detected "
            "explicit label or ARIA name"
        )

    # Carousel presence is context, not proof of outdated design.
    carousel_elements = soup.find_all(
        lambda tag: (
            tag.name in {"div", "section", "ul", "ol"}
            and (
                "carousel" in " ".join(tag.get("class", [])).lower()
                or "slider" in " ".join(tag.get("class", [])).lower()
                or tag.get("aria-roledescription", "").lower() == "carousel"
            )
        )
    )

    carousel_evidence = []
    for element in carousel_elements[:5]:
        carousel_evidence.append({
            "tag": element.name,
            "classes": element.get("class", [])[:5],
            "aria_roledescription": element.get("aria-roledescription"),
            "text_sample": _element_text(element),
        })

    return {
        "h1_count": len(h1_tags),
        "h1_text": [_element_text(tag) for tag in h1_tags[:3]],
        "h2_count": len(h2_tags),
        "h2_text": [_element_text(tag) for tag in h2_tags[:5]],
        "total_images": len(images),
        "images_missing_alt": len(missing_alt),
        "has_primary_cta": has_cta,
        "cta_candidates": cta_candidates[:6],
        "has_favicon": has_favicon,
        "copyright_year": copyright_year,
        "form_control_count": len(inputs),
        "unlabeled_controls": unlabeled_controls[:8],
        "carousel_detected": bool(carousel_evidence),
        "carousel_evidence": carousel_evidence,
        "issues": issues,
    }


def build_design_skeleton(
    soup: BeautifulSoup,
    css_data: Dict[str, Any],
    ui_data: Dict[str, Any],
    load_time: float,
    has_ssl: bool,
    is_mobile_viewport: bool,
) -> str:
    """Build compact, evidence-focused context for the LLM critic."""

    title = (
        soup.title.get_text(strip=True)
        if soup.title else "Unknown"
    )[:100]

    h1_text = ui_data.get("h1_text", [])
    h2_text = ui_data.get("h2_text", [])

    nav = soup.find("nav")
    nav_links = (
        [_element_text(link) for link in nav.find_all("a") if _element_text(link)]
        if nav else []
    )[:6]

    first_paragraph = soup.find("p")
    hero_copy = (
        _element_text(first_paragraph)
        if first_paragraph else "Not detected"
    )

    css_evidence = {
        "media_query_count": css_data.get("media_query_count"),
        "modern_layout_detected": css_data.get("uses_modern_layout"),
        "float_usage_count": css_data.get("float_usage_count"),
        "important_count": css_data.get("important_count"),
        "inline_styled_elements": css_data.get("inline_styled_elements"),
        "has_css_variables": css_data.get("has_css_variables"),
        "rigid_width_count": css_data.get("rigid_width_count"),
        "deprecated_tags": css_data.get("deprecated_presentation_tags", []),
        "font_families": css_data.get("font_families", []),
        "font_sizes_sample": css_data.get("font_sizes_sample", []),
        "line_heights_sample": css_data.get("line_heights_sample", []),
        "carousel_detected": css_data.get("carousel_detected", False),
        "carousel_evidence": css_data.get("carousel_evidence", []),
    }

    ui_evidence = {
        "h1": h1_text,
        "h2": h2_text,
        "navigation_links": nav_links,
        "cta_candidates": ui_data.get("cta_candidates", []),
        "has_favicon": ui_data.get("has_favicon"),
        "copyright_year": ui_data.get("copyright_year"),
        "image_count": ui_data.get("total_images"),
        "images_missing_alt": ui_data.get("images_missing_alt"),
        "form_control_count": ui_data.get("form_control_count"),
        "unlabeled_controls": ui_data.get("unlabeled_controls", []),
        "carousel_detected": ui_data.get("carousel_detected", False),
        "carousel_evidence": ui_data.get("carousel_evidence", []),
    }

    vitals = {
        # This is the fetch duration passed into the function, not
        # necessarily browser-rendered user-perceived load time.
        "page_fetch_seconds": load_time,
        "ssl_valid": has_ssl,
        "mobile_viewport_meta_detected": is_mobile_viewport,
    }

    return (
        f"Page title: {title}\n"
        f"Technical observations: {vitals}\n"
        f"CSS observations: {css_evidence}\n"
        f"DOM/UI observations: {ui_evidence}\n"
        f"First paragraph sample: {hero_copy}\n"
        "Limitations: DOM extraction cannot establish rendered visual quality, "
        "actual mobile behavior, or whether a carousel looks dated. Treat "
        "heuristic misses as unverified until supported by direct evidence."
    )
