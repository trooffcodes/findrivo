"""
extraction/parsers/social_parser.py
Detects social links, extracts handles, and inspects public profile metadata
(OpenGraph bio, title, follower stats, secondary emails) without using a browser.
"""

from typing import Dict, List, Optional, Set
from urllib.parse import urlparse
from bs4 import BeautifulSoup

from ..models import SocialProfile
from ..utils import clean_url, validate_email, validate_phone
from core.logger import app_logger, track_performance

SOCIAL_PLATFORMS: Dict[str, str] = {
    "linkedin.com": "LinkedIn",
    "twitter.com": "X/Twitter",
    "x.com": "X/Twitter",
    "facebook.com": "Facebook",
    "instagram.com": "Instagram",
    "youtube.com": "YouTube",
    "github.com": "GitHub",
    "tiktok.com": "TikTok",
}

DISCARD_PATTERNS = [
    "/share",
    "/intent",
    "/sharer",
    "/status/",
    "/hashtag/",
    "/post/",
    "/search",
]


class SocialParser:
    """Extracts social media links from HTML and enriches them via lightweight inspection."""

    @classmethod
    def discover_socials(cls, soup: BeautifulSoup, raw_same_as: Optional[List[str]] = None) -> List[SocialProfile]:
        urls_to_evaluate: Set[str] = set()

        if raw_same_as:
            for s in raw_same_as:
                urls_to_evaluate.add(s)

        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            for domain in SOCIAL_PLATFORMS:
                if domain in href.lower():
                    urls_to_evaluate.add(href)

        profiles: List[SocialProfile] = []
        for url in urls_to_evaluate:
            profile = cls._normalize_social_url(url)
            if profile:
                profiles.append(profile)

        # Deduplicate profiles by platform and handle
        unique_profiles: Dict[str, SocialProfile] = {}
        for p in profiles:
            key = f"{p.platform}:{p.handle or p.url}"
            if key not in unique_profiles:
                unique_profiles[key] = p

        return list(unique_profiles.values())

    @classmethod
    def _normalize_social_url(cls, raw_url: str) -> Optional[SocialProfile]:
        cleaned = clean_url(raw_url)
        parsed = urlparse(cleaned)
        netloc = parsed.netloc.lower()

        # Exclude social sharing intent endpoints
        for pattern in DISCARD_PATTERNS:
            if pattern in parsed.path.lower():
                return None

        for domain, platform_name in SOCIAL_PLATFORMS.items():
            if domain in netloc:
                path_segments = [p for p in parsed.path.split("/") if p]
                if not path_segments:
                    return None

                handle = path_segments[-1]
                # Filter out generic directories
                if handle.lower() in ("company", "in", "group", "home", "channel"):
                    if len(path_segments) >= 2:
                        handle = path_segments[-1]
                    else:
                        return None

                return SocialProfile(
                    platform=platform_name,
                    url=cleaned,
                    handle=handle,
                )

        return None

    @classmethod
    @track_performance("enrich_social_profile")
    def enrich_profile(cls, profile: SocialProfile, fetcher) -> SocialProfile:
        """
        Visits the public social profile with lightweight HTTP GET.
        Pulls OpenGraph description/title, which often carries the profile bio,
        external business email, and phone number.
        """
        # Skip LinkedIn enrichment if desired, as it blocks raw HTTP GET with login walls
        if profile.platform == "LinkedIn":
            return profile

        html_content, status, _ = fetcher.fetch(profile.url)
        if not html_content or status >= 400:
            return profile

        soup = BeautifulSoup(html_content, "html.parser")

        # 1. Inspect OpenGraph & meta description tags
        description_text = ""
        for meta in soup.find_all("meta"):
            prop = meta.get("property", "").lower()
            name = meta.get("name", "").lower()
            content = meta.get("content", "").strip()

            if prop in ("og:description", "twitter:description") or name == "description":
                if len(content) > len(description_text):
                    description_text = content

        if description_text:
            profile.bio = description_text

            # 2. Extract secondary emails from social bio
            from .contact_parser import EMAIL_REGEX, PHONE_REGEX
            for email in EMAIL_REGEX.findall(description_text):
                valid = validate_email(email)
                if valid and valid not in profile.extra_emails:
                    profile.extra_emails.append(valid)

            # 3. Extract secondary phones from social bio
            for phone in PHONE_REGEX.findall(description_text):
                valid = validate_phone(phone)
                if valid and valid not in profile.extra_phones:
                    profile.extra_phones.append(valid)

        app_logger.info("Enriched %s profile for handle: %s", profile.platform, profile.handle)
        return profile