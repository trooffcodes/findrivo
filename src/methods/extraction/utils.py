"""
extraction/utils.py
String sanitation, regex validation, and Cloudflare email deobfuscation.
"""

import html
import re
from typing import List, Optional, Set
from urllib.parse import urljoin, urlparse

# File extensions commonly found in href or img src that false-match email patterns
BANNED_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "webp", "svg", "css", "js",
    "pdf", "woff", "woff2", "ttf", "eot", "mp4", "webm"
}

# Domains often embedded in scripts/trackers that are not legitimate business emails
BANNED_EMAIL_DOMAINS = {
    "example.com", "domain.com", "email.com", "sentry.io",
    "wixpress.com", "shopify.com", "wordpress.org", "schema.org",
    "gravatar.com", "w3.org", "github.com", "google.com"
}


def clean_url(url: str, base_url: Optional[str] = None) -> str:
    """Normalizes and resolves relative URLs, stripping fragments and trailing slashes."""
    if not url:
        return ""
    if base_url:
        url = urljoin(base_url, url)
    parsed = urlparse(url)
    cleaned = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
    if parsed.query:
        cleaned += f"?{parsed.query}"
    return cleaned


def is_same_domain(url1: str, url2: str) -> bool:
    """Checks whether two URLs share the same base domain / registered host."""
    netloc1 = urlparse(url1).netloc.lower().removeprefix("www.")
    netloc2 = urlparse(url2).netloc.lower().removeprefix("www.")
    return netloc1 == netloc2 or netloc1.endswith(f".{netloc2}") or netloc2.endswith(f".{netloc1}")


def decode_cloudflare_email(cf_hex: str) -> str:
    """
    Decodes emails protected by Cloudflare's email protection (__cf_email__).
    Cloudflare uses a simple XOR cipher where the first byte is the XOR key.
    """
    try:
        r = int(cf_hex[:2], 16)
        email_chars = [
            chr(int(cf_hex[i : i + 2], 16) ^ r)
            for i in range(2, len(cf_hex), 2)
        ]
        return "".join(email_chars)
    except Exception:
        return ""


def clean_extracted_text(text: str) -> str:
    """Decodes HTML entities and condenses multiple whitespace characters."""
    if not text:
        return ""
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def validate_email(email: str) -> Optional[str]:
    """Validates email format and filters out false positives and tracker noise."""
    if not email:
        return None
    email = email.strip().lower().rstrip(".,;")
    
    # Must contain @ and a domain dot
    if "@" not in email or "." not in email:
        return None

    # Check extension
    ext = email.rsplit(".", 1)[-1]
    if ext in BANNED_EXTENSIONS:
        return None

    parts = email.split("@")
    if len(parts) != 2:
        return None

    user, domain = parts
    if domain in BANNED_EMAIL_DOMAINS:
        return None
    
    # Exclude dummy/placeholder values
    if user in {"name", "user", "username", "email", "test", "yourname", "info@yourdomain"}:
        return None

    # Length sanity
    if len(email) < 5 or len(email) > 100:
        return None

    return email


def validate_phone(phone_str: str) -> Optional[str]:
    """Cleans and validates phone strings, eliminating year/date/hex strings."""
    if not phone_str:
        return None

    raw = clean_extracted_text(phone_str)
    
    # Ignore dates like 2024-05-12 or 2026/10/19
    if re.match(r"^\d{4}[-/]\d{2}[-/]\d{2}$", raw):
        return None

    # Keep only digits and a potential leading '+'
    digits_only = re.sub(r"\D", "", raw)
    if not (7 <= len(digits_only) <= 15):
        return None

    # Discard repeated digits or sequential test numbers like 0000000000, 123456789
    if len(set(digits_only)) == 1 or digits_only in "123456789012345":
        return None

    # Standardize spaces and symbols
    normalized = re.sub(r"[^\d+()\-\s.]", "", raw).strip()
    return normalized