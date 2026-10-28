"""
methods/exporter.py
Thread-safe, atomic streaming CSV and JSON lead exporter for FindRivo.
Flushes each lead to disk immediately to prevent data loss.

Column set includes every relevant extracted field (contacts, audit findings,
evidence/confidence metadata, and timestamps). Missing values stay empty
cells — no fake placeholders — and formula-injection-prone text is neutralized
while the in-application data model keeps original values.
"""

import csv
import json
import os
import re
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import Config
from core.logger import app_logger, track_performance

_IST = timezone(timedelta(hours=5, minutes=30))
_DEFAULT_EXPORT_DIR = Config.EXPORT_DIR

_FILENAME_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _csv_safe(value: str) -> str:
    """Neutralize spreadsheet formula injection in text cells."""
    if not value:
        return value
    if value[0] in ("=", "+", "@", "\t", "\r", "\n"):
        return "'" + value
    if value[0] == "-" and not re.match(r"^-\d[\d.,:]*$", value):
        return "'" + value
    return value


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def slugify_label(label: str, max_length: int = 48) -> str:
    """Readable, filesystem-safe slug for export filenames."""
    slug = _FILENAME_SLUG_RE.sub("-", (label or "").strip().lower()).strip("-")
    return slug[:max_length].strip("-") or "campaign"


def build_export_filename(label: str, output_dir: Optional[str] = None, extension: str = "csv") -> str:
    """Descriptive filename: findrivo_leads_<label>_<utc-timestamp>.csv

    The timestamp makes repeated exports with the same label unambiguous.
    """
    stamp = datetime.now(_IST).strftime("%Y%m%d_%H%M%S")
    name = f"findrivo_leads_{slugify_label(label)}_{stamp}.{extension}"
    if output_dir:
        return str(Path(output_dir) / name)
    return name


def _join(values: Any) -> str:
    if isinstance(values, list):
        return "; ".join(str(v) for v in values if v not in (None, ""))
    if values is None:
        return ""
    return str(values)


def _flag(value: Any, when_true="YES", when_false="NO") -> str:
    return when_true if value else when_false


def _defects_of(lead: Dict[str, Any]) -> List[str]:
    value = lead.get("audit_defects") or lead.get("primary_defects") or lead.get("issues_found") or []
    return value if isinstance(value, list) else [str(value)]


def _hooks_of(lead: Dict[str, Any]) -> List[str]:
    hooks = lead.get("personalization_hooks") or []
    return hooks if isinstance(hooks, list) else [str(hooks)]


def _verdict_of(lead: Dict[str, Any]) -> str:
    return str(lead.get("design_verdict") or (lead.get("redesign_qualification") or {}).get("design_verdict") or "")


def _needs_redesign_of(lead: Dict[str, Any]) -> Any:
    value = lead.get("needs_redesign")
    if value is None:
        value = (lead.get("redesign_qualification") or {}).get("needs_redesign", False)
    return value


def _viability_of(lead: Dict[str, Any]) -> Any:
    value = lead.get("viability_score")
    if value is None:
        value = (lead.get("redesign_qualification") or {}).get("viability_score")
    return value


def _critic_confidence_of(lead: Dict[str, Any]) -> Any:
    value = lead.get("critic_confidence_score")
    if value is None:
        value = (lead.get("redesign_qualification") or {}).get("confidence_score")
    return value


def _ssl_days_of(lead: Dict[str, Any]) -> Any:
    value = lead.get("ssl_days_remaining")
    if value is None:
        value = (lead.get("ssl_details") or {}).get("days_remaining")
    return value


def _response_seconds_of(lead: Dict[str, Any]) -> Any:
    response = lead.get("response_time") or {}
    if isinstance(response, dict) and response:
        return response.get("seconds")
    if lead.get("load_time_seconds"):
        return lead.get("load_time_seconds")
    return ""


def _response_meta_of(lead: Dict[str, Any]) -> Dict[str, Any]:
    response = lead.get("response_time")
    return response if isinstance(response, dict) else {}


def _social_values(socials: Any, platforms: List[str]) -> Dict[str, str]:
    urls = {p: "" for p in platforms}
    if isinstance(socials, list):
        for profile in socials:
            platform = str(profile.get("platform") or "").lower()
            if platform in urls and not urls[platform]:
                urls[platform] = str(profile.get("url") or "")
    return urls


def _csv_from_social(lead: Dict[str, Any], key: str) -> str:
    direct = str(lead.get(key) or "")
    if direct:
        return direct
    social_links = lead.get("socials") or []
    if isinstance(social_links, list):
        wanted = key.replace("_url", "")
        for profile in social_links:
            if str(profile.get("platform") or "").lower() == wanted:
                return str(profile.get("url") or "")
    return ""


def _csv_number(value: Any) -> str:
    """Render a numeric field; None stays empty, real zeros are preserved."""
    if value is None:
        return ""
    try:
        return str(int(value)) if float(value) == int(float(value)) else str(value)
    except (TypeError, ValueError):
        return str(value)


EXPORT_FIELDNAMES = [
    "lead_id",
    "company_name",
    "niche",
    "search_location",
    "formatted_address",
    "primary_phone",
    "primary_email",
    "all_emails",
    "all_phones",
    "website_url",
    "listed_website",
    "website_status",
    "needs_redesign",
    "redesign_viability_score",
    "critic_confidence_score",
    "design_verdict",
    "audit_defects",
    "page_load_seconds",
    "is_mobile_responsive",
    "is_ssl_secured",
    "ssl_days_remaining",
    "response_time_seconds",
    "response_time_basis",
    "response_time_samples",
    "response_time_method",
    "google_rating",
    "reviews_count",
    "google_maps_url",
    "contact_form_url",
    "instagram_url",
    "linkedin_url",
    "facebook_url",
    "twitter_url",
    "company_one_liner",
    "company_description",
    "core_value_proposition",
    "personalization_hook_1",
    "personalization_hook_2",
    "suggested_outreach_angle",
    "ai_cold_pitch_hook",
    "recommended_service_angle",
    "extraction_confidence",
    "confidence_level",
    "opportunity_score",
    "opportunity_reasons",
    "outreach_suitability",
    "evidence_summary",
    "data_sources",
    "field_conflicts",
    "browser_recovered",
    "page_weight_mb",
    "extracted_at",
]


class CSVLeadExporter:
    """Thread-safe streaming exporter flushing leads to disk upon qualification."""

    def __init__(self, output_dir: str = _DEFAULT_EXPORT_DIR, filename: Optional[str] = None):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.row_count = 0

        if filename:
            if Path(filename).name != filename:
                raise ValueError("CSV filename must not contain path separators")
            self.filepath = self.output_dir / filename
        else:
            self.filepath = self.output_dir / build_export_filename("campaign")

        self._initialize_file()

    def _initialize_file(self) -> None:
        with self.lock:
            if not self.filepath.exists() or self.filepath.stat().st_size == 0:
                with open(self.filepath, mode="w", newline="", encoding="utf-8-sig") as f:
                    writer = csv.DictWriter(f, fieldnames=EXPORT_FIELDNAMES, extrasaction="ignore")
                    writer.writeheader()
                app_logger.info("Initialized lead export CSV at: %s", self.filepath)

    @staticmethod
    def _format_lead(lead: Dict[str, Any], lead_id: int) -> Dict[str, str]:
        primary_email = lead.get("email") or ""
        all_emails = lead.get("all_emails") or []
        if not all_emails and primary_email:
            all_emails = [primary_email]

        primary_phone = lead.get("phone") or ""
        all_phones = lead.get("all_phones") or []
        if not all_phones and primary_phone:
            all_phones = [primary_phone]

        hooks = _hooks_of(lead)

        response_meta = _response_meta_of(lead)

        row = {
            "lead_id": str(lead_id),
            "company_name": str(lead.get("name") or lead.get("company_name") or ""),
            "niche": str(lead.get("niche") or lead.get("category") or ""),
            "search_location": str(lead.get("search_location") or lead.get("location") or ""),
            "formatted_address": str(lead.get("address") or lead.get("formatted_address") or ""),
            "primary_phone": primary_phone,
            "primary_email": primary_email,
            "all_emails": _join(all_emails),
            "all_phones": _join(all_phones),
            "website_url": str(lead.get("website") or ""),
            "listed_website": str(lead.get("listed_website") or ""),
            "website_status": str(lead.get("website_status") or ""),
            "needs_redesign": _flag(_needs_redesign_of(lead)),
            "redesign_viability_score": _csv_number(_viability_of(lead)),
            "critic_confidence_score": _csv_number(_critic_confidence_of(lead)),
            "design_verdict": _verdict_of(lead),
            "audit_defects": _join(_defects_of(lead)),
            "page_load_seconds": f"{_as_float(_response_seconds_of(lead)):.2f}",
            "is_mobile_responsive": _flag(lead.get("is_mobile_viewport")),
            "is_ssl_secured": _flag(lead.get("has_ssl")),
            "ssl_days_remaining": _csv_number(_ssl_days_of(lead)),
            "response_time_seconds": _csv_number(_response_seconds_of(lead)),
            "response_time_basis": str(response_meta.get("basis") or ""),
            "response_time_samples": str(response_meta.get("samples") or ""),
            "response_time_method": str(response_meta.get("method") or ""),
            "google_rating": str(lead.get("rating") or ""),
            "reviews_count": str(lead.get("reviews") or ""),
            "google_maps_url": str(lead.get("maps_link") or ""),
            "contact_form_url": str(lead.get("contact_form") or ""),
            "instagram_url": _csv_from_social(lead, "instagram"),
            "linkedin_url": _csv_from_social(lead, "linkedin"),
            "facebook_url": _csv_from_social(lead, "facebook"),
            "twitter_url": _csv_from_social(lead, "twitter"),
            "company_one_liner": str(lead.get("one_liner") or ""),
            "company_description": str(lead.get("company_description") or ""),
            "core_value_proposition": str(lead.get("core_value_proposition") or ""),
            "personalization_hook_1": hooks[0] if len(hooks) > 0 else "",
            "personalization_hook_2": hooks[1] if len(hooks) > 1 else "",
            "suggested_outreach_angle": str(lead.get("suggested_outreach_angle") or ""),
            "ai_cold_pitch_hook": str(lead.get("ai_cold_pitch_hook") or lead.get("hook") or ""),
            "recommended_service_angle": str(lead.get("recommended_service_angle") or lead.get("service_angle") or ""),
            "extraction_confidence": str(lead.get("extraction_confidence") or ""),
            "confidence_level": str(lead.get("confidence_level") or ""),
            "opportunity_score": str(lead.get("opportunity_score") or ""),
            "opportunity_reasons": _join(lead.get("opportunity_reasons")),
            "outreach_suitability": str(lead.get("outreach_suitability") or ""),
            "evidence_summary": str(lead.get("evidence_summary") or ""),
            "data_sources": str(lead.get("data_sources") or ""),
            "field_conflicts": _join(lead.get("field_conflicts")),
            "browser_recovered": _flag(lead.get("browser_extraction_used")),
            "page_weight_mb": str(lead.get("page_weight_mb") or ""),
            "extracted_at": str(lead.get("timestamp") or datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S")),
        }
        return {key: _csv_safe(value) for key, value in row.items()}

    @track_performance("csv_append_lead")
    def append_lead(self, lead: Dict[str, Any]) -> str:
        with self.lock:
            self.row_count += 1
            row_data = self._format_lead(lead, self.row_count)
            last_err: Optional[Exception] = None
            for attempt in range(max(1, Config.CSV_WRITE_RETRIES)):
                try:
                    with open(self.filepath, mode="a", newline="", encoding="utf-8-sig") as f:
                        writer = csv.DictWriter(f, fieldnames=EXPORT_FIELDNAMES, extrasaction="ignore")
                        writer.writerow(row_data)
                        f.flush()
                        os.fsync(f.fileno())
                    last_err = None
                    break
                except PermissionError as err:
                    # Almost always the file being open in Excel: retry briefly,
                    # then preserve the row in a sidecar file instead of losing
                    # the lead or crashing the job.
                    last_err = err
                    if attempt < Config.CSV_WRITE_RETRIES - 1:
                        time.sleep(min(2.0, 0.4 * (attempt + 1)))
            if last_err is not None:
                recovery_path = self.filepath.with_name(
                    f"{self.filepath.stem}_recovered.csv"
                )
                self._append_to(recovery_path, row_data, write_header=True)
                app_logger.warning(
                    "CSV locked (close it in Excel): lead #%d written to recovery file %s",
                    self.row_count, recovery_path,
                )

        app_logger.info("Exported lead #%d to CSV: %s", self.row_count, row_data["company_name"])
        return str(self.filepath)

    def _append_to(self, path: Path, row_data: Dict[str, str], write_header: bool = False) -> None:
        needs_header = write_header and (not path.exists() or path.stat().st_size == 0)
        with open(path, mode="a", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=EXPORT_FIELDNAMES, extrasaction="ignore")
            if needs_header:
                writer.writeheader()
            writer.writerow(row_data)
            f.flush()
            os.fsync(f.fileno())


def export_leads_to_json(leads: List[Dict[str, Any]], filepath: str) -> str:
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(leads, f, indent=2, ensure_ascii=False)
    return str(path)
