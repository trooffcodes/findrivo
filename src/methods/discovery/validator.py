"""
Business Data Validator & Normalization Engine
"""

import re


def normalize_phone(phone_str: str) -> str:
    if not phone_str:
        return ""
    p = str(phone_str).strip()
    if not p or p.lower() in ["none", "n/a", "null", "-", "no phone"]:
        return ""
    
    digits = re.sub(r'[^\d]', '', p)
    # Format standard 10-digit US phone numbers: (XXX) XXX-XXXX
    if len(digits) == 10:
        return f"({digits[0:3]}) {digits[3:6]}-{digits[6:10]}"
    # US format with leading country code: +1 (XXX) XXX-XXXX
    if len(digits) == 11 and digits.startswith('1'):
        return f"+1 ({digits[1:4]}) {digits[4:7]}-{digits[7:11]}"
    if len(digits) >= 7:
        return "+" + digits if p.startswith('+') else digits
    return p


def parse_reviews_count(reviews_val) -> int:
    if not reviews_val:
        return 0
    r_str = str(reviews_val).strip().upper().replace(',', '')
    try:
        if 'K' in r_str:
            return int(float(r_str.replace('K', '').strip()) * 1000)
        if 'M' in r_str:
            return int(float(r_str.replace('M', '').strip()) * 1000000)
        match = re.search(r'(\d+)', r_str)
        if match:
            return int(match.group(1))
    except Exception:
        pass
    return 0


def parse_rating_val(rating_val) -> float:
    if not rating_val:
        return 0.0
    try:
        val = float(str(rating_val).strip())
        if 0.0 <= val <= 5.0:
            return round(val, 1)
    except Exception:
        pass
    return 0.0


def normalize_url(url_str: str) -> str:
    if not url_str:
        return ""
    u = str(url_str).strip()
    if not u or u.lower() in ["none", "n/a", "null", "-", "no website"]:
        return ""
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u


def validate_email_address(email_str: str) -> str:
    if not email_str or '@' not in str(email_str):
        return ""
    em = str(email_str).lower().strip()
    rejected_domains = [
        "example.com", "example.org", "domain.com", "email.com", "sample.com",
        "sentry.io", "wixpress.com", "bugsnag.com", "schema.org", "wordpress.org"
    ]
    if any(em.endswith("@" + d) or ("@" + d) in em for d in rejected_domains):
        return ""
    if any(em.startswith(p) for p in ["example@", "test@", "user@", "name@"]):
        return ""
    if any(b in em for b in ["%", "\\", "..", "http", "www", ".png", ".jpg", ".jpeg", ".svg"]):
        return ""
    if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', em):
        return ""
    return em


def process_raw_lead(raw_lead: dict) -> dict:
    cleaned = dict(raw_lead)
    cleaned["phone"] = normalize_phone(raw_lead.get("phone", ""))
    cleaned["website"] = normalize_url(raw_lead.get("website", ""))
    cleaned["email"] = validate_email_address(raw_lead.get("email", ""))
    cleaned["rating"] = parse_rating_val(raw_lead.get("rating", 0))
    cleaned["reviews"] = parse_reviews_count(raw_lead.get("reviews", 0))
    return cleaned