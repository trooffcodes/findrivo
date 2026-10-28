"""
extraction/extractor.py
Main engine orchestrating the crawling process:
- Scans homepage
- Identifies and prioritizes high-value contact subpages
- Extracts contacts, addresses, forms, and Schema.org metadata
- Enriches discovered social profiles
"""

from typing import List, Set
from urllib.parse import urlparse
from bs4 import BeautifulSoup

from core.logger import app_logger, track_performance
from .fetcher import LightweightFetcher
from .models import BusinessExtractionResult, ContactInfo, SocialProfile
from .parsers.contact_parser import ContactParser
from .parsers.meta_parser import MetaParser
from .parsers.social_parser import SocialParser
from .utils import clean_url, is_same_domain

# High-priority subpage keyword heuristics
PRIORITY_KEYWORDS = [
    "contact", "about", "team", "support", "impressum",
    "reach-us", "legal", "terms", "leadership", "office",
]


class BusinessExtractor:
    """Crawls a domain using lightweight HTTP requests to gather all available business contacts."""

    def __init__(self, max_pages: int = 6, enrich_socials: bool = True):
        self.max_pages = max_pages
        self.enrich_socials = enrich_socials
        self.fetcher = LightweightFetcher()

    @track_performance("business_extraction_pipeline")
    def extract(self, start_url: str) -> BusinessExtractionResult:
        if not start_url.startswith(("http://", "https://")):
            start_url = f"https://{start_url}"

        app_logger.info("Starting business contact extraction for: %s", start_url)

        # 1. Fetch Homepage
        html_content, status, final_url = self.fetcher.fetch(start_url)
        if not html_content:
            app_logger.error("Failed to load initial website: %s (Status: %d)", start_url, status)
            return self._empty_result(start_url)

        domain = urlparse(final_url).netloc
        base_url = f"{urlparse(final_url).scheme}://{domain}"

        # Setup tracking sets
        visited_urls: Set[str] = set()
        crawled_pages: List[str] = []
        emails_acc: Set[str] = set()
        phones_acc: Set[str] = set()
        addresses_acc: Set[str] = set()
        forms_acc: Set[str] = set()
        socials_acc: List[SocialProfile] = []
        legal_ids_acc: dict = {}

        # 2. Parse Homepage
        hp_soup = BeautifulSoup(html_content, "html.parser")
        visited_urls.add(clean_url(final_url))
        crawled_pages.append(final_url)

        meta_info = MetaParser.parse(hp_soup)
        company_name = meta_info.get("company_name") or domain
        description = meta_info.get("description")

        # Ingest schema-based findings
        emails_acc.update(meta_info.get("schema_emails", []))
        phones_acc.update(meta_info.get("schema_phones", []))
        addresses_acc.update(meta_info.get("schema_addresses", []))
        legal_ids_acc.update(meta_info.get("legal_identifiers", {}))

        # Ingest DOM findings
        emails_acc.update(ContactParser.extract_emails(hp_soup, html_content))
        phones_acc.update(ContactParser.extract_phone_numbers(hp_soup, html_content))
        addresses_acc.update(ContactParser.extract_addresses(hp_soup))
        forms_acc.update(ContactParser.extract_contact_forms(hp_soup, final_url))

        # Discover socials
        socials_acc.extend(
            SocialParser.discover_socials(hp_soup, meta_info.get("schema_same_as", []))
        )

        # 3. Discover high-priority internal links
        links_to_crawl = self._discover_priority_links(hp_soup, final_url, visited_urls)

        # 4. Crawl priority pages within budget; stop early once the core
        # contact material is complete so we never fetch pages we don't need.
        for url in links_to_crawl[: self.max_pages - 1]:
            app_logger.info("Crawling secondary priority page: %s", url)
            sub_html, sub_status, sub_url = self.fetcher.fetch(url)
            visited_urls.add(clean_url(url))

            if not sub_html or sub_status >= 400:
                continue

            crawled_pages.append(sub_url)
            sub_soup = BeautifulSoup(sub_html, "html.parser")

            sub_meta = MetaParser.parse(sub_soup)
            emails_acc.update(sub_meta.get("schema_emails", []))
            phones_acc.update(sub_meta.get("schema_phones", []))
            addresses_acc.update(sub_meta.get("schema_addresses", []))
            legal_ids_acc.update(sub_meta.get("legal_identifiers", {}))

            emails_acc.update(ContactParser.extract_emails(sub_soup, sub_html))
            phones_acc.update(ContactParser.extract_phone_numbers(sub_soup, sub_html))
            addresses_acc.update(ContactParser.extract_addresses(sub_soup))
            forms_acc.update(ContactParser.extract_contact_forms(sub_soup, sub_url))

            socials_acc.extend(
                SocialParser.discover_socials(sub_soup, sub_meta.get("schema_same_as", []))
            )

            if emails_acc and phones_acc and forms_acc:
                app_logger.info(
                    "Stopping crawl early: core contact data complete after %d page(s)",
                    len(crawled_pages),
                )
                break

        # 5. Deduplicate and enrich Socials
        unique_socials: dict = {}
        for s in socials_acc:
            k = f"{s.platform}:{s.handle or s.url}"
            if k not in unique_socials:
                unique_socials[k] = s

        final_socials: List[SocialProfile] = list(unique_socials.values())

        if self.enrich_socials:
            enriched_list: List[SocialProfile] = []
            for sp in final_socials:
                enriched = SocialParser.enrich_profile(sp, self.fetcher)
                # Ingest any extra contacts found on social bios
                emails_acc.update(enriched.extra_emails)
                phones_acc.update(enriched.extra_phones)
                enriched_list.append(enriched)
            final_socials = enriched_list

        self.fetcher.close()

        contacts = ContactInfo(
            emails=sorted(list(emails_acc)),
            phone_numbers=sorted(list(phones_acc)),
            addresses=sorted(list(addresses_acc)),
            contact_forms=sorted(list(forms_acc)),
            socials=final_socials,
            legal_identifiers=legal_ids_acc,
        )

        app_logger.info(
            "Extraction completed for %s. Found: %d emails, %d phones, %d socials.",
            domain,
            len(contacts.emails),
            len(contacts.phone_numbers),
            len(contacts.socials),
        )

        return BusinessExtractionResult(
            domain=domain,
            base_url=base_url,
            company_name=company_name,
            description=description,
            contacts=contacts,
            pages_crawled=crawled_pages,
            raw_metadata=meta_info,
        )

    def _discover_priority_links(
        self, soup: BeautifulSoup, current_url: str, visited: Set[str]
    ) -> List[str]:
        """Discovers internal links matching contact/about/team heuristics."""
        scored_links: List[tuple] = []

        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            resolved = clean_url(href, current_url)

            if not resolved or resolved in visited:
                continue

            if not is_same_domain(current_url, resolved):
                continue

            link_text = a.get_text(" ").lower()
            path = urlparse(resolved).path.lower()

            # Score by priority relevance
            score = 0
            for idx, kw in enumerate(PRIORITY_KEYWORDS):
                if kw in path:
                    score += (len(PRIORITY_KEYWORDS) - idx) * 2
                if kw in link_text:
                    score += len(PRIORITY_KEYWORDS) - idx

            if score > 0:
                scored_links.append((score, resolved))

        # Sort descending by priority score and deduplicate
        scored_links.sort(key=lambda x: x[0], reverse=True)
        seen = set(visited)
        result = []
        for _, link in scored_links:
            if link not in seen:
                seen.add(link)
                result.append(link)

        return result

    def _empty_result(self, url: str) -> BusinessExtractionResult:
        domain = urlparse(url).netloc or url
        return BusinessExtractionResult(
            domain=domain,
            base_url=url,
            company_name=domain,
            description="",
            contacts=ContactInfo(),
            pages_crawled=[],
            raw_metadata={},
        )