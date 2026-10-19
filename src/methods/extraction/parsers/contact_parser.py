"""
extraction/parsers/contact_parser.py
Specialized parser extracting emails, phones, contact forms, physical addresses,
and Cloudflare encrypted emails using regex, DOM heuristics, and pattern decoders.
"""

import html
import re
from typing import List, Set, Tuple
from bs4 import BeautifulSoup

from ..utils import (
    clean_extracted_text,
    clean_url,
    decode_cloudflare_email,
    validate_email,
    validate_phone,
)

# Standard international & regional phone regex
PHONE_REGEX = re.compile(
    r"""(
        (?:(?:\+|00)\d{1,3}[\s.-]?)?     # International dial code: +1, +44, 0049
        (?:\(?\d{2,5}\)?[\s.-]?)?        # Area code: (800), (020)
        \d{3,5}[\s.-]?\d{3,5}             # Main subscriber digits
    )""",
    re.VERBOSE,
)

# Strict email regex
EMAIL_REGEX = re.compile(
    r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+",
    re.IGNORECASE,
)

# Obfuscated patterns like: name [at] company [dot] com
OBFUSCATED_EMAIL_REGEX = re.compile(
    r"([a-zA-Z0-9_.+-]+)\s*(?:\[at\]|\(at\)|@)\s*([a-zA-Z0-9-]+)\s*(?:\[dot\]|\(dot\)|\.)\s*([a-zA-Z0-9-.]+)",
    re.IGNORECASE,
)


class ContactParser:
    """Parses contact points directly from the DOM and raw HTML payload."""

    @classmethod
    def extract_emails(cls, soup: BeautifulSoup, raw_html: str) -> List[str]:
        emails: Set[str] = set()

        # 1. Cloudflare Encrypted Emails
        cf_tags = soup.find_all(class_="__cf_email__")
        for tag in cf_tags:
            data_cf = tag.get("data-cfemail")
            if data_cf:
                decoded = decode_cloudflare_email(data_cf)
                validated = validate_email(decoded)
                if validated:
                    emails.add(validated)

        # 2. mailto: links in anchors
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().startswith("mailto:"):
                clean_target = href[7:].split("?")[0].strip()
                validated = validate_email(clean_target)
                if validated:
                    emails.add(validated)

        # 3. Plaintext and HTML entity matches
        decoded_html = html.unescape(raw_html)
        for match in EMAIL_REGEX.findall(decoded_html):
            validated = validate_email(match)
            if validated:
                emails.add(validated)

        # 4. Obfuscated matches (e.g. contact [at] site [dot] com)
        for user, domain, tld in OBFUSCATED_EMAIL_REGEX.findall(decoded_html):
            reconstructed = f"{user}@{domain}.{tld}"
            validated = validate_email(reconstructed)
            if validated:
                emails.add(validated)

        return sorted(list(emails))

    @classmethod
    def extract_phone_numbers(cls, soup: BeautifulSoup, raw_html: str) -> List[str]:
        phones: Set[str] = set()

        # 1. tel: anchors
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().startswith("tel:"):
                val = href[4:].split("?")[0].strip()
                validated = validate_phone(val)
                if validated:
                    phones.add(validated)

        # 2. Search contact sections and footers to reduce false positives
        candidate_blocks: List[str] = []
        for selector in ["footer", "header", ".contact", "#contact", "address"]:
            elements = soup.select(selector)
            for el in elements:
                candidate_blocks.append(el.get_text(" "))

        # If no targeted blocks found, search entire clean body
        if not candidate_blocks:
            body = soup.find("body")
            if body:
                candidate_blocks.append(body.get_text(" "))

        for block in candidate_blocks:
            for match in PHONE_REGEX.findall(block):
                validated = validate_phone(match)
                if validated:
                    phones.add(validated)

        return sorted(list(phones))

    @classmethod
    def extract_addresses(cls, soup: BeautifulSoup) -> List[str]:
        addresses: Set[str] = set()

        # Explicit <address> HTML tags
        for addr_tag in soup.find_all("address"):
            txt = clean_extracted_text(addr_tag.get_text(" "))
            if len(txt) > 10:
                addresses.add(txt)

        # Footer postal regex search (Street, St, Blvd, Ave, Suite, Road, Zip/Postal code)
        postal_pattern = re.compile(
            r"\b\d{1,5}\s+[\w\s.,]{3,40}\b(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Suite|Ste|Floor|Drive|Dr|Way)\b[\w\s.,]*\b\d{4,6}\b",
            re.IGNORECASE,
        )

        footer = soup.find("footer")
        if footer:
            for match in postal_pattern.findall(footer.get_text(" ")):
                cleaned = clean_extracted_text(match)
                if len(cleaned) > 12:
                    addresses.add(cleaned)

        return sorted(list(addresses))

    @classmethod
    def extract_contact_forms(cls, soup: BeautifulSoup, current_url: str) -> List[str]:
        forms: Set[str] = set()

        for form in soup.find_all("form"):
            action = form.get("action", "")
            method = form.get("method", "post").lower()
            text_desc = form.get_text(" ").lower()

            # Identify if form has email/message fields
            has_input = False
            for inp in form.find_all(["input", "textarea"]):
                inp_type = inp.get("type", "").lower()
                inp_name = inp.get("name", "").lower()
                if inp_type in ("email", "text") or inp_name in ("email", "message", "contact"):
                    has_input = True
                    break

            if has_input or "message" in text_desc or "submit" in text_desc:
                resolved = clean_url(action, current_url) if action else current_url
                forms.add(resolved)

        return sorted(list(forms))