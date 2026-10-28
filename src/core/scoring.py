"""
core/scoring.py
Evidence-based lead scoring for FindRivo.

This module separates four previously conflated concepts:

1. Extraction confidence — how well the collected evidence supports the
   extracted facts (NOT how confident an LLM phrase sounded).
2. Opportunity score    — how promising the business is as a web-design
   prospect, based on explicit, checkable criteria.
3. Audit findings       — directly observed website issues (kept in the audit
   module; referenced here only for what was actually observed).
4. Outreach suitability — whether there is enough reliable evidence to contact
   the business with a specific, factual angle.

Scoring is intentionally simple and interpretable. Every contribution has a
stated meaning; no formula exists to look scientific.
"""

from typing import Any, Dict, List, Optional, Tuple

# --- Evidence statuses -------------------------------------------------
VERIFIED = "verified"        # Directly observed from a primary source that owns the data.
UNVERIFIED = "unverified"    # From a secondary source; plausible but unchecked.
INFERRED = "inferred"        # Produced by an AI/heuristic, not observed anywhere.
CONFLICTING = "conflicting"  # Primary sources disagree; no single value chosen.
MISSING = "missing"          # Never found in any source.

# Relative weight of each status in confidence math. Absence of evidence
# contributes nothing: missing data must not raise confidence.
_STATUS_WEIGHT = {
    VERIFIED: 1.0,
    UNVERIFIED: 0.5,
    INFERRED: 0.25,
    CONFLICTING: 0.2,
    MISSING: 0.0,
}

# Relevance of each field to "how much do we actually know about this lead".
_FIELD_WEIGHTS = {
    "name": 1.0,
    "website": 1.5,
    "phone": 1.2,
    "email": 1.2,
    "address": 0.8,
    "description": 0.6,
}

# Quality ordering used when two claims compete for the same field.
# Higher number = higher-quality evidence.
_STATUS_RANK = {
    VERIFIED: 3,
    UNVERIFIED: 2,
    INFERRED: 1,
    CONFLICTING: 0,
    MISSING: -1,
}

_CONFIDENCE_LEVELS = (
    ("high", 75),
    ("medium", 45),
    # below 45 -> "low"
)

# Confidence is capped below 100: collection completing is not the same as
# every fact being independently confirmed.
_MAX_EXTRACTION_CONFIDENCE = 90

_OPPORTUNITY_CAP = 95


class EvidenceTracker:
    """Records the source and quality status of each extracted field.

    A claim from a better source replaces a weaker one; a discovered conflict
    is recorded instead of silently overwriting a confirmed value.
    """

    def __init__(self) -> None:
        self.field_status: Dict[str, str] = {}
        self.field_sources: Dict[str, str] = {}
        self.conflicts: Dict[str, List[str]] = {}

    def claim(
        self,
        field: str,
        value: Any,
        status: str,
        source: str,
        existing_model=None,
    ) -> None:
        """Record where a field value came from.

        Policy for overlapping claims:
          * a higher-quality status on the same field replaces what is there;
          * an equal-quality value from a different source kind that disagrees
            is a genuine conflict — recorded, existing value kept;
          * a lower-quality value never replaces a confirmed one; the
            disagreement is noted under `conflicts`.
        """
        status = status if status in _STATUS_WEIGHT else UNVERIFIED
        current_status = self.field_status.get(field, MISSING)
        current_source = self.field_sources.get(field)

        record = existing_model or {}
        old_value = record.get(field)
        values_differ = bool(
            value
            and old_value
            and str(value).strip() != str(old_value).strip()
        )

        if current_status == MISSING:
            self.field_status[field] = status
            self.field_sources[field] = source
            return

        if _status_rank(status) > _status_rank(current_status):
            # Better evidence replaces weaker evidence; note both values when
            # they actually differ so nothing is silently lost.
            if values_differ:
                self.conflicts.setdefault(field, []).append(str(old_value)[:200])
            self.field_status[field] = status
            self.field_sources[field] = source
            return

        if _status_rank(status) == _status_rank(current_status):
            if values_differ and not _same_source_kind(current_source, source):
                self.conflicts.setdefault(field, []).append(str(value)[:200])
                self.field_status[field] = CONFLICTING
            return

        # Weaker incoming evidence: keep the confirmed value.
        if values_differ:
            self.conflicts.setdefault(field, []).append(str(value)[:200])

    def claim_absence(self, field: str, source: str) -> None:
        """Record that a primary source directly shows the field does not exist
        (e.g., a business listing with no website anchor). Verified absence is
        evidence, distinct from "we simply failed to find anything".
        """
        if self.field_status.get(field, MISSING) == MISSING:
            self.field_status[field] = VERIFIED
            self.field_sources[field] = f"{source}:absence"

    def summary(self) -> Dict[str, str]:
        return dict(self.field_status)

    def sources(self) -> Dict[str, str]:
        return dict(self.field_sources)

    def conflict_list(self) -> Dict[str, List[str]]:
        return {field: values for field, values in self.conflicts.items() if values}


def _status_rank(status: str) -> int:
    return _STATUS_RANK.get(status, 1)


def _same_source_kind(a: Optional[str], b: Optional[str]) -> bool:
    """Treat source types that may legitimately restatement as equivalent."""
    group = {a or "", b or ""}
    official = {"official_website_http", "official_website_browser"}
    maps = {"google_maps"}
    if group <= official:
        return True
    if group <= maps:
        return True
    return a == b


# --- Extraction confidence ---------------------------------------------

def extraction_confidence(evidence: EvidenceTracker) -> Tuple[int, str]:
    """Return (0-100, level) describing how well evidence supports the facts.

    The value is weighted coverage of key fields by their evidence status.
    Unverified facts count half, inferred facts a quarter, absent facts zero.
    """
    if not evidence.field_status:
        return 0, "low"

    weighted_total = 0.0
    achieved = 0.0
    for field, weight in _FIELD_WEIGHTS.items():
        weighted_total += weight
        status = evidence.field_status.get(field, MISSING)
        achieved += weight * _STATUS_WEIGHT.get(status, 0.0)

    if weighted_total <= 0:
        return 0, "low"

    raw = int(round(100.0 * achieved / weighted_total))
    score = min(raw, _MAX_EXTRACTION_CONFIDENCE)

    level = "low"
    for name, floor in _CONFIDENCE_LEVELS:
        if score >= floor:
            level = name
            break
    return score, level


# --- Opportunity score ---------------------------------------------------

def opportunity_score(
    lead: Dict[str, Any],
    evidence: EvidenceTracker,
    response_meta: Optional[Dict[str, Any]] = None,
) -> Tuple[int, List[str]]:
    """Return (0-100, reasons) for how promising the lead is as a prospect.

    Explicit criteria (each one stated in the returned reasons):

    * A business with NO official website is a strong opportunity, but only if
      the absence is verified against the source listing data (not merely
      inferred because nothing was found).
    * A live website with directly observed, significant defects is a strong
      redesign opportunity. Trivial observations (favicon, image alt text,
      copyright year) do not qualify on their own.
    * Slow response time only counts when the measurement is considered
      reliable (multiple samples); otherwise it is labelled inconclusive and
      contributes nothing.
    * Weak, stale, assumed, or contradictory evidence reduces the score.
    """
    website = (lead.get("website") or "").strip()
    rating = _safe_float(lead.get("rating"))
    reviews = _safe_int(lead.get("reviews"))
    response_meta = response_meta or {}
    reasons: List[str] = []
    score = 0

    offline_strength = (
        rating >= 4.0
        and reviews >= 25
    )

    if not website:
        # Absence of the website itself is only trustworthy when the primary
        # listing source had no website record at all (vs. merely listing a
        # social profile URL that was filtered out).
        if evidence.field_status.get("website") == VERIFIED:
            # Here "verified" absence means the listing has no website entry.
            score += 55
            reasons.append(
                "No official website listed on the business's primary listing "
                f"(rating {rating:g}* from {reviews} reviews)"
            )
            if offline_strength:
                score += 15
                reasons.append("Strong offline reputation despite no web presence")
        else:
            score += 20
            reasons.append(
                "No official website recovered, but absence is unverified "
                "(listing source was unclear or secondary)"
            )
        if not offline_strength:
            score -= 5
            reasons.append("Offline reputation signals are weak, reducing prospect strength")
        if evidence.conflict_list():
            score -= 5
            reasons.append("Contact data has unresolved conflicts")
        return _clamp(max(score, 0)), reasons

    # Website exists. Opportunity is driven by observed, significant defects.
    if lead.get("is_live"):
        verdict = (lead.get("design_verdict") or "").lower()
        significant = _significant_defects(lead, response_meta)

        if significant and verdict == "outdated":
            score += 60
            reasons.append(
                "Website is outdated by observed evidence: "
                + "; ".join(significant[:3])
            )
        elif significant:
            score += 40
            reasons.append("Website has observed technical issues: " + "; ".join(significant[:3]))
        elif verdict == "outdated":
            # The critic judges it outdated but no direct evidence backs it.
            score += 25
            reasons.append("Critique verdict 'outdated' without directly observed defects")
        else:
            score += 5
            reasons.append("Website is functional and modern; low redesign opportunity")

        # Response-time contribution is gated on measurement reliability.
        rt_seconds = _safe_float(lead.get("load_time_seconds"))
        reliable = bool(response_meta.get("reliable") or response_meta.get("samples", 0) >= 2)
        if rt_seconds > 3.0 and reliable:
            score += 8
            reasons.append(
                f"Median HTTP response time {rt_seconds:.1f}s across "
                f"{response_meta.get('samples', 2)} samples is unusually slow"
            )
        elif rt_seconds > 3.0 and response_meta.get("inconclusive"):
            reasons.append("Response time measurement inconclusive (single noisy sample); ignored for scoring")
    else:
        # Website URL known but the site does not respond.
        if evidence.field_status.get("website") in (VERIFIED, UNVERIFIED):
            score += 10

    # Evidence-quality adjustments.
    if evidence.field_status.get("website") == UNVERIFIED:
        score -= 10
        reasons.append("Website URL only verified indirectly")
    if evidence.conflict_list():
        score -= 5
        reasons.append("Contact data has unresolved conflicts")
    if lead.get("is_live") and 0.0 < rating < 3.0:
        score -= 5
        reasons.append("Low rating suggests a struggling business; outreach may still help")
    if evidence.field_status.get("description") == MISSING:
        score -= 3
        reasons.append("No company description recovered, limiting personalization")

    return _clamp(max(0, min(score, _OPPORTUNITY_CAP))), reasons


def _significant_defects(lead: Dict[str, Any], response_meta: Dict[str, Any]) -> List[str]:
    """Filter observed defects down to significant, directly checkable ones.

    Minor signals (favicon, alt text, copyright) alone do not make a redesign
    prospect, per the scoring requirements.
    """
    significant: List[str] = []
    if not lead.get("is_mobile_viewport"):
        significant.append("Missing mobile viewport meta")
    if not lead.get("has_ssl"):
        significant.append("No SSL certificate")
    broken = lead.get("broken_links_count") or 0
    if isinstance(broken, int) and broken >= 2:
        significant.append(f"{broken} broken internal links sampled")
    ssl_days = lead.get("ssl_days_remaining")
    if isinstance(ssl_days, int) and 0 <= ssl_days <= 14:
        significant.append(f"SSL certificate expires in {ssl_days} days")
    load = _safe_float(lead.get("load_time_seconds"))
    if load > 3.0 and response_meta.get("reliable") is True:
        significant.append(f"Slow median response time {load:.1f}s (reliable measurement)")
    return significant


# --- Outreach suitability ----------------------------------------------

def outreach_suitability(
    lead: Dict[str, Any],
    evidence: EvidenceTracker,
) -> Tuple[str, str]:
    """Return (suitability, explanation).

    Levels:
      ready          — verified contact channel plus a factual outreach angle.
      needs_verification — some evidence exists but is not confirmed enough to
                           treat as reliable for a personalized pitch.
      insufficient   — too little evidence to justify a specific pitch.
    """
    verified_facts: List[str] = [
        field
        for field in ("phone", "email", "website", "name", "address")
        if evidence.field_status.get(field) == VERIFIED
        and lead.get(field)
    ]
    has_verified_contact = any(
        evidence.field_status.get(field) == VERIFIED and lead.get(field)
        for field in ("phone", "email")
    )
    has_any_contact_claim = any(
        field in evidence.field_status and field in ("phone", "email", "contact_form")
        for field in evidence.field_status
    )
    factual_angle = bool(lead.get("primary_defects") or lead.get("needs_redesign"))

    if has_verified_contact and (factual_angle or len(verified_facts) >= 2):
        channel = "email" if lead.get("email") else "phone"
        return (
            "ready",
            f"Verified contact via {channel} and factual outreach grounds "
            "observed in the audit/listing data.",
        )
    if has_verified_contact:
        return (
            "needs_verification",
            "A contact method exists but outreach-hungry facts (defects, "
            "website status) are not confirmed enough for a personalized angle.",
        )
    if has_any_contact_claim or verified_facts:
        return (
            "needs_verification",
            "Contact details exist but none are confirmed by a reliable "
            "source yet; verifying before outreach is recommended.",
        )
    return (
        "insufficient",
        "Nothing about this business is verified yet; contacting now is not "
        "justified by the available evidence.",
    )


# --- Browser fallback policy (used by the extraction layer) -------------

def should_browser_fallback(
    lead: Dict[str, Any],
    config: Any,
) -> Tuple[bool, str]:
    """Decide whether Playwright rendering could plausibly recover useful data.

    Conditions (kept deliberately explicit):
      * feature enabled  — nothing renders otherwise
      * live website     — browser cannot help an offline/unlisted site
      * promising lead   — clear quality thresholds from configuration
      * important info missing — email, phone, or contact page unresolved
    Social profile URLs or other optional fields do NOT trigger a rendering
    run: browsers do not conjure such links more reliably than HTTP parsing.
    """
    if not getattr(config, "BROWSER_FALLBACK_ENABLED", False):
        return False, "browser fallback feature disabled"

    website = (lead.get("website") or "").strip()
    if not website:
        return False, "business has no official website to render"
    if lead.get("is_live") is not True:
        return False, "website is not confirmed live; browser rendering cannot help"

    rating = _safe_float(lead.get("rating"))
    reviews = _safe_int(lead.get("reviews"))
    promising = (
        rating >= config.BROWSER_FALLBACK_MIN_RATING
        or reviews >= config.BROWSER_FALLBACK_MIN_REVIEWS
    )
    if not promising:
        return (
            False,
            f"lead is not promising enough (rating={rating:g}, reviews={reviews})",
        )

    missing_important = []
    if not (lead.get("email") or "").strip():
        missing_important.append("email")
    if not (lead.get("phone") or "").strip():
        missing_important.append("phone")
    if not (lead.get("contact_form") or "").strip():
        missing_important.append("contact_form")

    if not missing_important:
        return False, "all important contact fields already extracted"

    # Rendering is plausible when heavy JS likely hides content (e.g., contact
    # page detected but empty, or extraction result had few crawled pages).
    pages_crawled = lead.get("pages_crawled") or []
    plausible = bool(pages_crawled) or bool(lead.get("http_extraction_attempted"))
    if not plausible:
        return False, "no HTTP evidence that rendering would help"

    return True, "important fields missing and site is live: " + ", ".join(missing_important)


# --- Shared small helpers ------------------------------------------------

def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _clamp(value: int) -> int:
    return max(0, min(int(value), 100))


def evidence_summary(evidence: EvidenceTracker) -> str:
    """Compact human-readable evidence map for CSV/UI display."""
    return "; ".join(
        f"{field}:{status}" for field, status in sorted(evidence.field_status.items())
    )


def aggregate_data_sources(lead: Dict[str, Any], evidence: EvidenceTracker) -> str:
    unique = {src for src in evidence.field_sources.values() if src}
    if lead.get("browser_extraction_used"):
        unique.add("browser_rendered")
    if lead.get("http_extraction_attempted"):
        unique.add("http_extracted")
    return ",".join(sorted(unique))
