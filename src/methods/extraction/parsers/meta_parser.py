"""
extraction/parsers/meta_parser.py
Extracts JSON-LD Schema.org graphs, OpenGraph tags, and HTML headers.
"""

import json
from typing import Any, Dict, List, Optional
from bs4 import BeautifulSoup
from core.logger import app_logger


class MetaParser:
    """Parses structured data (JSON-LD), OpenGraph, and title metadata from HTML."""

    @staticmethod
    def parse(soup: BeautifulSoup) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "title": "",
            "description": "",
            "company_name": "",
            "schema_emails": [],
            "schema_phones": [],
            "schema_addresses": [],
            "schema_same_as": [],
            "legal_identifiers": {},
        }

        # 1. Title
        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            result["title"] = title_tag.string.strip()

        # 2. Meta description & OpenGraph
        for meta in soup.find_all("meta"):
            name = meta.get("name", "").lower()
            prop = meta.get("property", "").lower()
            content = meta.get("content", "").strip()

            if not content:
                continue

            if name == "description" or prop == "og:description":
                if not result["description"]:
                    result["description"] = content
            elif prop in ("og:site_name", "application-name"):
                if not result["company_name"]:
                    result["company_name"] = content

        # 3. JSON-LD Schema extraction
        scripts = soup.find_all("script", type="application/ld+json")
        for script in scripts:
            if not script.string:
                continue
            try:
                data = json.loads(script.string)
                MetaParser._recurse_schema_nodes(data, result)
            except (json.JSONDecodeError, TypeError):
                continue

        # Dedup items
        result["schema_emails"] = list(set(result["schema_emails"]))
        result["schema_phones"] = list(set(result["schema_phones"]))
        result["schema_addresses"] = list(set(result["schema_addresses"]))
        result["schema_same_as"] = list(set(result["schema_same_as"]))

        return result

    @staticmethod
    def _recurse_schema_nodes(node: Any, result: Dict[str, Any]):
        """Recursively parses Schema.org graphs for contact and organization properties."""
        if isinstance(node, list):
            for item in node:
                MetaParser._recurse_schema_nodes(item, result)
            return

        if not isinstance(node, dict):
            return

        # Check @graph arrays
        if "@graph" in node and isinstance(node["@graph"], list):
            MetaParser._recurse_schema_nodes(node["@graph"], result)

        node_type = str(node.get("@type", ""))
        org_types = {"Organization", "LocalBusiness", "Corporation", "Store", "MedicalBusiness"}

        if any(ot in node_type for ot in org_types) or "name" in node:
            if not result["company_name"] and "name" in node:
                if isinstance(node["name"], str):
                    result["company_name"] = node["name"].strip()

            # Email
            if "email" in node:
                em = node["email"]
                if isinstance(em, str):
                    result["schema_emails"].append(em.replace("mailto:", "").strip())
                elif isinstance(em, list):
                    result["schema_emails"].extend([e.replace("mailto:", "").strip() for e in em if isinstance(e, str)])

            # Phone
            if "telephone" in node:
                tel = node["telephone"]
                if isinstance(tel, str):
                    result["schema_phones"].append(tel.replace("tel:", "").strip())
                elif isinstance(tel, list):
                    result["schema_phones"].extend([t.replace("tel:", "").strip() for t in tel if isinstance(t, str)])

            # Address
            if "address" in node:
                addr = node["address"]
                if isinstance(addr, str):
                    result["schema_addresses"].append(addr.strip())
                elif isinstance(addr, dict):
                    parts = [
                        addr.get("streetAddress"),
                        addr.get("addressLocality"),
                        addr.get("addressRegion"),
                        addr.get("postalCode"),
                        addr.get("addressCountry"),
                    ]
                    formatted = ", ".join([str(p).strip() for p in parts if p])
                    if formatted:
                        result["schema_addresses"].append(formatted)

            # SameAs (Social profiles)
            if "sameAs" in node:
                same_as = node["sameAs"]
                if isinstance(same_as, str):
                    result["schema_same_as"].append(same_as.strip())
                elif isinstance(same_as, list):
                    result["schema_same_as"].extend([s.strip() for s in same_as if isinstance(s, str)])

            # Tax & VAT IDs
            for tax_key in ("vatID", "taxID", "leiCode"):
                if tax_key in node and isinstance(node[tax_key], str):
                    result["legal_identifiers"][tax_key] = node[tax_key].strip()

        # Recurse down sub-dictionaries
        for val in node.values():
            if isinstance(val, (dict, list)):
                MetaParser._recurse_schema_nodes(val, result)