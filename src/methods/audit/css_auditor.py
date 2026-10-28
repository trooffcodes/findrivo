
import re
from typing import Any, Dict, List
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from core.logger import app_logger, track_performance
from core.net_guard import safe_request

_MAX_STYLESHEET_FETCH = 2
_MAX_CSS_BYTES = 120_000


def _unique_matches(pattern: str, text: str, limit: int = 8) -> List[str]:
    """Return a small, deduplicated sample of regex matches."""
    found = re.findall(pattern, text, re.IGNORECASE)
    unique = []

    for item in found:
        value = item.strip().strip("\"'")
        if value and value not in unique:
            unique.append(value)
        if len(unique) >= limit:
            break

    return unique


@track_performance("css_audit")
def audit_css(
    soup: BeautifulSoup,
    base_url: str,
    session: requests.Session,
) -> Dict[str, Any]:
    """Collect CSS implementation signals without treating them as visual proof."""
    issues: List[str] = []
    css_sources: List[str] = []

    # Embedded styles.
    for style_tag in soup.find_all("style"):
        css_text = style_tag.get_text()
        if css_text.strip():
            css_sources.append(css_text)

    # Fetch a bounded number of linked stylesheets.
    link_tags = soup.find_all(
        "link",
        rel=lambda value: value and "stylesheet" in value,
    )
    fetched_count = 0

    for link in link_tags:
        if fetched_count >= _MAX_STYLESHEET_FETCH:
            break

        href = (link.get("href") or "").strip()
        if not href or href.startswith(("data:", "javascript:")):
            continue

        resolved_url = urljoin(base_url, href)

        try:
            with safe_request(
                session,
                "GET",
                resolved_url,
                timeout=2.5,
                stream=True,
            ) as response:
                if response.status_code != 200:
                    continue

                raw_chunk = response.raw.read(
                    _MAX_CSS_BYTES,
                    decode_content=True,
                )
                css_text = raw_chunk.decode("utf-8", errors="ignore")

                if css_text.strip():
                    css_sources.append(css_text)
                    fetched_count += 1

        except Exception as err:
            app_logger.debug(
                "Failed fetching stylesheet %s: %s",
                resolved_url,
                err,
            )

    combined_css = "\n".join(css_sources)

    media_query_count = len(
        re.findall(r"@media\b", combined_css, re.IGNORECASE)
    )

    uses_modern_layout = bool(
        re.search(
            r"display\s*:\s*(?:inline-)?(?:flex|grid)\b",
            combined_css,
            re.IGNORECASE,
        )
    )

    float_usage_count = len(
        re.findall(
            r"float\s*:\s*(?:left|right)\b",
            combined_css,
            re.IGNORECASE,
        )
    )

    important_count = len(
        re.findall(r"!important\b", combined_css, re.IGNORECASE)
    )

    has_css_vars = bool(
        re.search(r"--[a-zA-Z0-9_-]+\s*:", combined_css)
    )

    inline_styled_elements = len(
        soup.find_all(lambda tag: tag.has_attr("style"))
    )

    rigid_width_count = len(
        re.findall(
            r"(?:max-)?width\s*:\s*"
            r"(?:960|980|1000|1024|1140|1200)px\b",
            combined_css,
            re.IGNORECASE,
        )
    )

    # Deprecated HTML presentation elements are directly observable.
    archaic_tags = soup.find_all(
        ["center", "font", "marquee", "blink", "strike"]
    )
    deprecated_tags = sorted({f"<{tag.name}>" for tag in archaic_tags})

    if deprecated_tags:
        issues.append(
            "Deprecated HTML presentation elements found: "
            + ", ".join(deprecated_tags)
        )

    # Extract font declarations as evidence, not as a design-quality verdict.
    font_families = _unique_matches(
        r"font-family\s*:\s*([^;{}]+)",
        combined_css,
    )

    font_sizes = _unique_matches(
        r"font-size\s*:\s*([^;{}]+)",
        combined_css,
        limit=10,
    )

    line_heights = _unique_matches(
        r"line-height\s*:\s*([^;{}]+)",
        combined_css,
        limit=8,
    )

    # Identify carousel/slider implementations; presence alone is not a defect.
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
        classes = element.get("class", [])
        carousel_evidence.append({
            "tag": element.name,
            "classes": classes[:5],
            "aria_roledescription": element.get("aria-roledescription"),
            "text_sample": element.get_text(" ", strip=True)[:100],
        })

    return {
        # Existing fields retained for compatibility.
        "media_query_count": media_query_count,
        "uses_modern_layout": uses_modern_layout,
        "float_usage_count": float_usage_count,
        "important_count": important_count,
        "has_css_variables": has_css_vars,
        "inline_styled_elements": inline_styled_elements,
        "stylesheets_analyzed": fetched_count + len([
            tag for tag in soup.find_all("style")
            if tag.get_text(strip=True)
        ]),
        "issues": issues,

        # Additional evidence for downstream analysis.
        "rigid_width_count": rigid_width_count,
        "deprecated_presentation_tags": deprecated_tags,
        "font_families": font_families,
        "font_sizes_sample": font_sizes,
        "line_heights_sample": line_heights,
        "carousel_detected": bool(carousel_evidence),
        "carousel_evidence": carousel_evidence,
    }
