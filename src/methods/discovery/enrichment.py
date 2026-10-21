"""
High-Speed Concurrent Website Enrichment
"""

import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, Set
import requests
import urllib3
from bs4 import BeautifulSoup
from validator import validate_email_address
import antidetect

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_SESSION = requests.Session()
_ADAPTER = requests.adapters.HTTPAdapter(pool_connections=30, pool_maxsize=30, max_retries=1)
_SESSION.mount("http://", _ADAPTER)
_SESSION.mount("https://", _ADAPTER)


def _extract_emails_from_html(soup: BeautifulSoup, text: str) -> Set[str]:
    found = set()
    for mailto in soup.select('a[href^="mailto:"]'):
        clean = mailto.get('href', '').replace('mailto:', '').split('?')[0].strip()
        val = validate_email_address(clean)
        if val:
            found.add(val)
    if not found:
        for em in re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text):
            val = validate_email_address(em)
            if val:
                found.add(val)
    return found


def _fetch_subpage_emails(url: str, headers: dict) -> Set[str]:
    try:
        r = _SESSION.get(url, headers=headers, timeout=2.5, verify=False)
        if r.status_code == 200:
            sp = BeautifulSoup(r.text, 'html.parser')
            return _extract_emails_from_html(sp, r.text)
    except Exception:
        pass
    return set()


def enrich_website_fast(website_url: str) -> Dict[str, Any]:
    data = {
        "website_status": "No Website",
        "email": "",
        "whatsapp": "",
        "facebook": "",
        "instagram": "",
        "linkedin": "",
        "twitter": "",
        "contact_form": ""
    }
    if not website_url or not website_url.strip():
        return data

    url = website_url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    headers = {"User-Agent": antidetect.get_random_user_agent()}

    try:
        resp = _SESSION.get(url, headers=headers, timeout=4.0, verify=False)
        if resp.status_code == 200:
            data["website_status"] = "Working"
            soup = BeautifulSoup(resp.text, 'html.parser')
            emails = _extract_emails_from_html(soup, resp.text)

            # Discover Socials
            for a in soup.find_all('a', href=True):
                href = a['href'].strip()
                h_low = href.lower()
                if any(w in h_low for w in ['wa.me', 'api.whatsapp.com', 'whatsapp:']) and not data["whatsapp"]:
                    data["whatsapp"] = href
                elif 'facebook.com' in h_low and 'sharer' not in h_low and not data["facebook"]:
                    data["facebook"] = href
                elif 'instagram.com' in h_low and not data["instagram"]:
                    data["instagram"] = href
                elif 'linkedin.com' in h_low and not data["linkedin"]:
                    data["linkedin"] = href
                elif ('twitter.com' in h_low or 'x.com' in h_low) and not data["twitter"]:
                    data["twitter"] = href
                elif any(k in h_low for k in ['/contact', 'contact-us']) and not data["contact_form"]:
                    data["contact_form"] = href if href.startswith('http') else urllib.parse.urljoin(url, href)

            # Parallel subpage scan if no email on homepage
            if not emails:
                sub_urls = [urllib.parse.urljoin(url, p) for p in ["/contact", "/contact-us", "/about"]]
                with ThreadPoolExecutor(max_workers=3) as pool:
                    results = pool.map(lambda u: _fetch_subpage_emails(u, headers), sub_urls)
                    for res in results:
                        if res:
                            emails.update(res)
                            break

            if emails:
                data["email"] = next(iter(emails))
        else:
            data["website_status"] = f"Broken ({resp.status_code})"
    except requests.exceptions.Timeout:
        data["website_status"] = "Timeout"
    except Exception:
        data["website_status"] = "Inaccessible"

    return data