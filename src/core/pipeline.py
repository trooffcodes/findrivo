"""
core/pipeline.py
FindRivo Autonomous Lead Generation Pipeline.
Decoupled Producer-Consumer Architecture:
  Producer Thread (Playwright Discovery) -> Worker Pool (Audit & Extraction) -> Streaming Consumer (SSE/CSV/CLI).
"""

import concurrent.futures
import json
import os
import queue
import re
import threading
import time
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple

from config import Config
from core import shutdown as shutdown_core
from core.scoring import (
    EvidenceTracker,
    INFERRED,
    UNVERIFIED,
    VERIFIED,
    aggregate_data_sources,
    evidence_summary,
    extraction_confidence,
    opportunity_score,
    outreach_suitability,
    should_browser_fallback,
)
from core.llm_client import LLMClient
from core.logger import app_logger, track_performance
from methods.audit import audit_website
from methods.extraction import BusinessExtractor, PersonalizationEngine
from methods.extraction import browser_fallback
from methods.discovery.lead_discovery import discover_businesses
from methods.exporter import CSVLeadExporter
from prompts.pitch_prompt import PITCH_SYSTEM_PROMPT
from prompts.planner_prompt import PLANNER_SYSTEM_PROMPT


def safe_browser_fallback(website_url: str) -> Optional[Dict[str, Any]]:
    """Run the bounded Playwright fallback, fully isolated from the job.

    Any failure returns None instead of raising, so one bad website cannot
    crash the discovery job (failures are memoized to avoid retry loops).
    """
    try:
        return browser_fallback.try_extract_within_budget(website_url)
    except Exception as err:
        app_logger.warning("Browser fallback error for %s: %s", website_url, err)
        return None


_browser_cleanup_registered = False


def _ensure_browser_cleanup_registered() -> None:
    """Register the shared fallback browser with the shutdown manager once."""
    global _browser_cleanup_registered
    if _browser_cleanup_registered:
        return
    shutdown_core.get_manager().register_cleanup(browser_fallback.shutdown_pool)
    _browser_cleanup_registered = True


def _merge_fallback_result(
    lead_record: Dict[str, Any],
    fallback_res: Dict[str, Any],
    evidence: EvidenceTracker,
    all_emails: List[Any],
    all_phones: List[Any],
) -> None:
    """Merge rendered-page findings without overwriting verified values.

    Higher-quality claims replace weaker ones; genuine disagreements are
    recorded as conflicts on the evidence tracker instead of silently
    overwriting an already-confirmed value.
    """
    emails = fallback_res.get("emails") or []
    phones = fallback_res.get("phone_numbers") or []
    forms = fallback_res.get("contact_forms") or []
    socials = fallback_res.get("socials") or []

    if emails:
        candidate = emails[0]
        if not lead_record.get("email"):
            lead_record["email"] = candidate
        all_emails.extend(emails)
        evidence.claim("email", candidate, VERIFIED, "official_website_browser", lead_record)
    if phones:
        candidate = phones[0]
        if not lead_record.get("phone"):
            lead_record["phone"] = candidate
        all_phones.extend(phones)
        evidence.claim("phone", candidate, VERIFIED, "official_website_browser", lead_record)
    if forms and not lead_record.get("contact_form"):
        lead_record["contact_form"] = forms[0]
        evidence.claim("contact_form", forms[0], VERIFIED, "official_website_browser", lead_record)
    if fallback_res.get("description") and not lead_record.get("company_description"):
        lead_record["company_description"] = fallback_res["description"]
        evidence.claim("description", fallback_res["description"], UNVERIFIED, "official_website_browser", lead_record)

    platforms = {
        "instagram": "instagram_url",
        "linkedin": "linkedin_url",
        "facebook": "facebook_url",
        "twitter": "twitter_url",
    }
    for profile in socials:
        if isinstance(profile, dict):
            platform = str(profile.get("platform") or "").lower()
            url_value = str(profile.get("url") or "")
        else:
            # Models are tolerated defensively even though browser_fallback
            # now serializes them to dicts.
            platform = str(getattr(profile, "platform", "") or "").lower()
            url_value = str(getattr(profile, "url", "") or "")
        key = platforms.get(platform)
        if key and not lead_record.get(key):
            lead_record[key] = url_value


class LeadGenPipeline:
    """Agentic pipeline orchestrator for FindRivo."""

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        concurrency: int = Config.MAX_AUDIT_CONCURRENCY,
    ):
        self.llm = llm_client or LLMClient()
        self.concurrency = max(1, min(concurrency, 10))
        # Social profile enrichment measured at 0-25s per lead (multi-hundred-KB
        # Facebook/Instagram/YouTube pages) for marginal value, so it is off by
        # default; direct contacts come from the business's own website.
        self.extractor = BusinessExtractor(max_pages=3, enrich_socials=False)
        self.personalizer = PersonalizationEngine(llm_client=self.llm)

    # ==========================================
    # 1. PLANNER AGENT
    # ==========================================

    @track_performance("pipeline_planner_agent")
    def plan(
        self,
        user_query: str,
        agency_services: Optional[str] = None,
        target_count: int = 10,
    ) -> Dict[str, Any]:
        """Deconstructs user input into execution parameters."""
        app_logger.info("Planner Agent parsing query: '%s'", user_query)
        agency_context = agency_services or "Modern mobile-first website redesign, performance speedup, and SEO."

        prompt = (
            f"User Goal: {user_query}\n"
            f"Agency Services: {agency_context}\n"
            f"Target Leads: {target_count}\n\n"
            "Produce the execution plan JSON matching the schema."
        )

        try:
            raw_response = self.llm.query(prompt=prompt, system_instruction=PLANNER_SYSTEM_PROMPT)
            clean = raw_response.strip()
            match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", clean)
            if match:
                clean = match.group(1).strip()
            plan_data = json.loads(clean)
        except Exception as e:
            app_logger.warning("Planner LLM fallback triggered: %s", e)
            plan_data = self._heuristic_planner(user_query, target_count)

        return self._validate_plan(plan_data, user_query, agency_context, target_count)

    @classmethod
    def _validate_plan(
        cls,
        plan_data: Any,
        user_query: str,
        agency_context: str,
        target_count: int,
    ) -> Dict[str, Any]:
        """Normalize LLM output so malformed or hostile values cannot reach workers."""
        if not isinstance(plan_data, dict):
            plan_data = cls._heuristic_planner(user_query, target_count)

        fallback = cls._heuristic_planner(user_query, target_count)

        industry = plan_data.get("industry")
        if not isinstance(industry, str) or not industry.strip():
            industry = fallback["industry"]
        location = plan_data.get("location")
        if not isinstance(location, str) or not location.strip():
            location = fallback["location"]

        try:
            requested_count = int(target_count)
        except (TypeError, ValueError):
            requested_count = Config.DEFAULT_LEAD_TARGET
        safe_count = max(1, min(requested_count, Config.MAX_TARGET_COUNT))

        defects = plan_data.get("target_defects")
        if not isinstance(defects, list):
            defects = fallback["target_defects"]
        defects = [str(item)[:120] for item in defects[:20] if str(item).strip()]

        queries = plan_data.get("search_queries")
        if not isinstance(queries, list):
            queries = fallback["search_queries"]
        queries = [str(item)[:300] for item in queries[:15] if str(item).strip()]

        # A single Maps feed tops out around ~100 candidates, so large targets
        # need several feeds. Pad with intent variations of the first query
        # when the planner returned too few distinct searches.
        min_queries = max(4, min(12, (safe_count + 14) // 15))
        if queries and len(queries) < min_queries:
            base = queries[0]
            for modifier in ("best ", "top rated ", "affordable ", "emergency ", "24/7 ", "family "):
                variant = f"{modifier}{base}"
                if variant not in queries:
                    queries.append(variant)
                if len(queries) >= min_queries:
                    break

        contacts = plan_data.get("must_have_contacts")
        if not isinstance(contacts, list):
            contacts = fallback["must_have_contacts"]
        contacts = [str(item)[:40] for item in contacts[:10] if str(item).strip()]

        return {
            "industry": industry.strip()[:120],
            "location": location.strip()[:160],
            "target_defects": defects,
            "must_have_contacts": contacts,
            "search_queries": queries,
            "min_relevance_score": plan_data.get("min_relevance_score", 70),
            "target_count": safe_count,
            "agency_services": agency_context,
        }

    @staticmethod
    def _heuristic_planner(user_query: str, target_count: int) -> Dict[str, Any]:
        parts = user_query.split(" in ")
        niche = parts[0].strip() if len(parts) == 2 else user_query.strip()
        location = parts[1].strip() if len(parts) == 2 else "Austin, TX"

        return {
            "industry": niche or "Dental Clinic",
            "location": location or "Austin, TX",
            "target_defects": ["missing_mobile_viewport", "slow_speed", "outdated_design", "no_ssl"],
            "must_have_contacts": ["email", "phone"],
            "search_queries": [f"{niche} in {location}"],
            "min_relevance_score": 70,
            "target_count": target_count,
        }

    # ==========================================
    # 2. WORKER AUDITING & ENRICHMENT
    # ==========================================

    @track_performance("process_single_lead")
    def _process_candidate(
        self,
        raw_lead: Dict[str, Any],
        agency_services: str,
        target_defects: List[str],
        cancel_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        business_name = raw_lead.get("name", "Unknown Business")
        website = (raw_lead.get("website") or "").strip()
        listed_website = (raw_lead.get("listed_website") or "").strip()
        lead_record = dict(raw_lead)
        lead_started = time.monotonic()
        all_emails = [raw_lead.get("email")] if raw_lead.get("email") else []
        all_phones = [raw_lead.get("phone")] if raw_lead.get("phone") else []
        evidence = EvidenceTracker()
        response_meta: Dict[str, Any] = {}

        # Distinct sources that contributed data for this lead (logged per lead).
        # --- Primary listing facts (Google Maps data owned by the business) ---
        if business_name:
            evidence.claim("name", business_name, VERIFIED, "google_maps", lead_record)
        if raw_lead.get("phone"):
            evidence.claim("phone", lead_record.get("phone"), VERIFIED, "google_maps", lead_record)
            all_phones.append(lead_record.get("phone"))
        if raw_lead.get("address"):
            evidence.claim("address", lead_record.get("address"), VERIFIED, "google_maps", lead_record)

        if website:
            # URL came from the listing; treat as unverified until observed.
            evidence.claim("website", website, UNVERIFIED, "google_maps", lead_record)
        elif listed_website:
            # The listing only pointed at a social profile; the absence of an
            # official website is thus plausible but unverified.
            evidence.claim("website", "", UNVERIFIED, "google_maps", lead_record)
        else:
            evidence.claim_absence("website", "google_maps")

        audit_res: Optional[Dict[str, Any]] = None
        extraction_res = None
        fallback_task = None  # Browser render handle; submitted early, collected late.

        # Step A: Audit (direct observation of the website itself)
        if not website:
            lead_record.update({
                "website_status": "No Website",
                "is_live": False,
                "has_ssl": False,
                "load_time_seconds": 0.0,
                "is_mobile_viewport": False,
                "needs_redesign": True,
                "viability_score": None,
                "design_verdict": "no-website",
                "audit_defects": ["No website listed on the business's primary listing."],
                "primary_defects": ["No official website available"],
            })
        else:
            app_logger.info("Lead step | name=%s | step=audit | website=%s", business_name, website)
            audit_res = audit_website(website, enable_llm_critic=True)
            redesign = audit_res.get("redesign_qualification", {})
            is_live = audit_res.get("is_live", False)
            response_meta = audit_res.get("response_time", {}) or {}

            needs_redesign = (
                not is_live
                or redesign.get("needs_redesign", False)
                or not audit_res.get("is_mobile_viewport", True)
                or not audit_res.get("has_ssl", True)
                or audit_res.get("load_time_seconds", 0) > 3.5
            )

            defects = redesign.get("primary_defects") or audit_res.get("issues_found", [])
            lead_record.update({
                "website_status": "Live" if is_live else f"Broken ({audit_res.get('load_time_seconds')}s)",
                "is_live": is_live,
                "has_ssl": audit_res.get("has_ssl", False),
                "ssl_days_remaining": audit_res.get("ssl_details", {}).get("days_remaining"),
                "load_time_seconds": audit_res.get("load_time_seconds", 0.0),
                "response_time": audit_res.get("response_time", {}),
                "is_mobile_viewport": audit_res.get("is_mobile_viewport", False),
                "needs_redesign": needs_redesign,
                "viability_score": redesign.get("viability_score", 85 if needs_redesign else 20),
                "critic_confidence_score": redesign.get("confidence_score"),
                "design_verdict": redesign.get("design_verdict", "outdated" if needs_redesign else "passable"),
                "audit_defects": defects[:3],
                "primary_defects": defects[:3],
                "page_weight_mb": (audit_res.get("page_weight") or {}).get("total_mb"),
            })

            if is_live:
                evidence.claim("website", website, VERIFIED, "audit_observed", lead_record)
            else:
                evidence.claim("website", website, UNVERIFIED, "audit_observed", lead_record)

            # Step B: Lightweight HTTP-first contact extraction.
            if is_live:
                try:
                    extraction_res = self.extractor.extract(website)
                    lead_record["http_extraction_attempted"] = True
                    lead_record["pages_crawled"] = extraction_res.pages_crawled
                    if extraction_res and extraction_res.contacts:
                        contacts = extraction_res.contacts
                        if contacts.emails:
                            all_emails.extend(contacts.emails)
                            if not lead_record.get("email"):
                                lead_record["email"] = contacts.emails[0]
                            evidence.claim(
                                "email", contacts.emails[0], VERIFIED,
                                "official_website_http", lead_record,
                            )
                        if contacts.phone_numbers:
                            all_phones.extend(contacts.phone_numbers)
                            if not lead_record.get("phone"):
                                lead_record["phone"] = contacts.phone_numbers[0]
                            evidence.claim(
                                "phone", contacts.phone_numbers[0], VERIFIED,
                                "official_website_http", lead_record,
                            )
                        if contacts.contact_forms and not lead_record.get("contact_form"):
                            lead_record["contact_form"] = contacts.contact_forms[0]
                            evidence.claim(
                                "contact_form", contacts.contact_forms[0], VERIFIED,
                                "official_website_http", lead_record,
                            )
                        if extraction_res.description and not lead_record.get("company_description"):
                            lead_record["company_description"] = extraction_res.description
                            evidence.claim(
                                "description", extraction_res.description, UNVERIFIED,
                                "official_website_http", lead_record,
                            )
                except Exception as err:
                    app_logger.warning("Deep extraction failure for %s: %s", website, err)

                # Step B2: Bounded Playwright fallback — only when the lead is
                # promising and important contact data is still missing. The
                # render is SUBMITTED here but only collected after the LLM
                # steps below, so the browser works in parallel instead of
                # stalling this lead (and the whole worker pool) for minutes.
                if extraction_res is not None:
                    gate_ok, gate_reason = should_browser_fallback(lead_record, Config)
                    app_logger.info(
                        "Browser fallback gate for %s: gate=%s reason=%s",
                        website, gate_ok, gate_reason,
                    )
                    if gate_ok and not (cancel_event and cancel_event.is_set()):
                        fallback_task = browser_fallback.submit_extract(website)

        lead_record["all_emails"] = sorted(set(e for e in all_emails if e))
        lead_record["all_phones"] = sorted(set(
            p for p in all_phones if p and str(p).strip() != str(lead_record.get("phone") or "").strip()
        ))

        # Step C + Step D: personalization and pitch generation are independent
        # of each other and are the last two network-bound steps, so they run
        # concurrently. After a cancellation request we skip both LLM calls and
        # use the deterministic fallbacks instead of burning quota on output
        # nobody will consume.
        cancelled = bool(cancel_event and cancel_event.is_set())
        use_llm = bool(website and lead_record.get("is_live")) and not cancelled

        def _fallback_personalization() -> None:
            lead_record["one_liner"] = f"Top-rated {raw_lead.get('category', 'business')} in {raw_lead.get('search_location', '')} without a website."
            lead_record["core_value_proposition"] = "Strong offline reputation lacking online conversion."
            lead_record["personalization_hooks"] = [f"Has {raw_lead.get('reviews', 0)} Google Maps reviews with no direct booking link."]
            lead_record["suggested_outreach_angle"] = "Capture local search traffic with a dedicated fast-loading website."
            evidence.claim(
                "description", lead_record["one_liner"], INFERRED, "llm_inference", lead_record,
            )

        def _run_personalization() -> None:
            if not use_llm:
                _fallback_personalization()
                return
            try:
                brief = self.personalizer.analyze_company(website, extraction_res)
                lead_record["one_liner"] = brief.one_liner
                lead_record["core_value_proposition"] = brief.core_value_proposition
                lead_record["personalization_hooks"] = brief.personalization_hooks
                lead_record["suggested_outreach_angle"] = brief.suggested_outreach_angle
                evidence.claim(
                    "description", brief.one_liner, INFERRED, "llm_inference", lead_record,
                )
            except Exception:
                _fallback_personalization()

        def _run_pitch() -> None:
            lead_record["ai_cold_pitch_hook"], lead_record["recommended_service_angle"] = self._generate_pitch(
                lead_name=business_name,
                defects=lead_record.get("audit_defects", []),
                services=[agency_services],
                location=raw_lead.get("search_location", "Local Area"),
                load_time_seconds=lead_record.get("load_time_seconds") or None,
                ssl_days_remaining=lead_record.get("ssl_days_remaining"),
                is_mobile_viewport=lead_record.get("is_mobile_viewport"),
                broken_links_count=(audit_res or {}).get("broken_links_count", 0) if audit_res else 0,
                page_weight_mb=lead_record.get("page_weight_mb"),
            )

        if use_llm:
            app_logger.info("Lead step | name=%s | step=personalization+pitch (llm)", business_name)
            pitch_thread = threading.Thread(target=_run_pitch, daemon=True)
            pitch_thread.start()
            try:
                _run_personalization()
            finally:
                pitch_thread.join()
        else:
            _run_personalization()
            _run_pitch()

        # Collect the browser fallback submitted before the LLM steps. By now
        # the render has usually finished in parallel; we only pay wall-clock
        # time for whatever is left of its budget.
        if fallback_task is not None:
            fallback_res = fallback_task.wait_result(timeout=Config.BROWSER_FALLBACK_TIMEOUT + 5)
            if fallback_res:
                lead_record["browser_extraction_used"] = True
                _merge_fallback_result(lead_record, fallback_res, evidence, all_emails, all_phones)
            else:
                lead_record["browser_extraction_attempted"] = True
            app_logger.info(
                "Browser fallback decision for %s: recovered=%s",
                website, bool(fallback_res),
            )

        # Step E: Separated, evidence-based scoring (replaces conflated values).
        (
            lead_record["extraction_confidence"],
            lead_record["confidence_level"],
        ) = extraction_confidence(evidence)
        (
            lead_record["opportunity_score"],
            lead_record["opportunity_reasons"],
        ) = opportunity_score(lead_record, evidence, response_meta)
        (
            lead_record["outreach_suitability"],
            lead_record["outreach_note"],
        ) = outreach_suitability(lead_record, evidence)

        lead_record["evidence"] = evidence.summary()
        lead_record["field_conflicts"] = evidence.conflict_list()
        lead_record["data_sources"] = aggregate_data_sources(lead_record, evidence)
        lead_record["evidence_summary"] = evidence_summary(evidence)
        # Legacy key: derived from the same closed evidence model so old
        # consumers keep working without conflating LLM phrasing confidence.
        lead_record.setdefault("confidence_score", lead_record["extraction_confidence"])

        app_logger.info(
            "Lead processed | name=%s | website=%s | live=%s | needs_redesign=%s | "
            "confidence=%s(%s) | opportunity=%s | outreach=%s | duration=%.1fs",
            business_name,
            website or "none",
            lead_record.get("is_live"),
            lead_record.get("needs_redesign"),
            lead_record.get("extraction_confidence"),
            lead_record.get("confidence_level"),
            lead_record.get("opportunity_score"),
            lead_record.get("outreach_suitability"),
            time.monotonic() - lead_started,
        )

        return lead_record

    def _generate_pitch(
        self,
        lead_name: str,
        defects: List[str],
        services: List[str],
        location: str,
        load_time_seconds: Optional[float] = None,
        ssl_days_remaining: Optional[int] = None,
        is_mobile_viewport: Optional[bool] = None,
        broken_links_count: int = 0,
        page_weight_mb: Optional[float] = None,
    ) -> Tuple[str, str]:
        payload = {
            "name": lead_name,
            "defects": defects if defects else ["Not mobile responsive", "Slow load time"],
            "load_time_seconds": load_time_seconds,
            "ssl_days_remaining": ssl_days_remaining,
            "is_mobile_viewport": is_mobile_viewport,
            "broken_links_count": broken_links_count,
            "page_weight_mb": page_weight_mb,
            "services": services,
            "location": location,
        }

        try:
            raw_response = self.llm.query(prompt=f"Input: {json.dumps(payload)}", system_instruction=PITCH_SYSTEM_PROMPT)
            clean = raw_response.strip()
            match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", clean)
            if match:
                clean = match.group(1).strip()
            data = json.loads(clean)
            return data.get("hook", ""), data.get("service_angle", "Website Redesign")
        except Exception:
            flaw = defects[0] if defects else "mobile layout defects"
            return (
                f"We noticed {lead_name}'s site experiences {flaw}, which impacts customer conversion in {location}.",
                "Website Redesign & Conversion Optimization",
            )

    # ==========================================
    # 3. DECOUPLED STREAMING PIPELINE
    # ==========================================

    def execute(
        self,
        user_query: str,
        agency_services: Optional[str] = None,
        target_count: int = 10,
        only_qualified_redesigns: bool = True,
        csv_filename: Optional[str] = None,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Generator[Dict[str, Any], None, None]:
        """
        Streams leads in real-time using decoupled Producer-Consumer concurrency.

        `cancel_event` lets a caller (for example, a disconnected SSE client)
        stop discovery and worker submission promptly.
        """
        cancel_event = cancel_event or threading.Event()
        shutdown_core.get_manager().register_cancel_event(cancel_event, "pipeline-job")
        _ensure_browser_cleanup_registered()
        plan = self.plan(user_query, agency_services, target_count)
        exporter = CSVLeadExporter(filename=csv_filename)
        # Second, parallel CSV: every processed lead (qualified or not).
        name = exporter.filepath.name
        stem, ext = os.path.splitext(name)
        all_exporter = CSVLeadExporter(filename=f"{stem}_all{ext}")
        exporter.all_leads_path = all_exporter.filepath
        app_logger.info("Qualified leads CSV: %s | All-leads CSV: %s", exporter.filepath, all_exporter.filepath)
        browser_fallback.reset_per_job_budget()

        niche = plan.get("industry", "Business")
        location = plan.get("location", "Austin, TX")
        target_defects = plan.get("target_defects", [])
        goal_leads = plan.get("target_count", target_count)

        # Use every search query the planner produced — one Maps feed per
        # query — instead of a single feed for the whole job. A single feed
        # tops out around a hundred candidates, which starves large targets.
        discovery_queries = [str(q).strip() for q in (plan.get("search_queries") or []) if str(q).strip()]
        if not discovery_queries:
            discovery_queries = [f"{niche} in {location}"]
        app_logger.info(
            "Discovery plan: %d queries for target of %d leads: %s",
            len(discovery_queries), goal_leads, discovery_queries,
        )

        raw_queue: queue.Queue = queue.Queue(maxsize=30)
        stop_event = threading.Event()

        # Producer Thread: Playwright Discovery
        def put_with_backpressure(item) -> bool:
            """Queue an item, but never block shutdown/cancellation indefinitely."""
            while not (stop_event.is_set() or cancel_event.is_set()):
                try:
                    raw_queue.put(item, timeout=0.2)
                    return True
                except queue.Full:
                    continue
            return False

        def handle_progress(item: Dict[str, Any]) -> None:
            """Forward lightweight discovery progress to interested callers.

            Progress items never become leads, rows, or persisted records; a
            callback failure must not be able to kill a running job. They are
            also mirrored into the app log so CLI runs (which have no callback)
            still show what discovery is doing moment to moment.
            """
            message = item.get("message")
            if message:
                app_logger.info("Discovery | %s", message)
            if not progress_callback:
                return
            try:
                progress_callback(item)
            except Exception as err:
                app_logger.debug("Progress callback error: %s", err)

        def discovery_producer():
            try:
                for lead in discover_businesses(
                    niche=niche,
                    location=location,
                    concurrency=Config.DISCOVERY_CONCURRENCY,
                    max_leads_per_location=max(goal_leads * 4, 40),
                    headless=Config.HEADLESS,
                    queries=discovery_queries,
                ):
                    if stop_event.is_set() or cancel_event.is_set():
                        break
                    if not put_with_backpressure(lead):
                        break
            except Exception as e:
                app_logger.error("Discovery producer encountered an error: %s", e)
            finally:
                put_with_backpressure(None)  # Sentinel (best effort)

        producer_thread = threading.Thread(target=discovery_producer, daemon=True)
        producer_thread.start()

        # Worker Pool
        qualified_count = 0
        active_futures = set()
        discovered_count = 0
        skipped_count = 0
        last_heartbeat = time.monotonic()

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                producer_finished = False

                while (
                    qualified_count < goal_leads
                    and not cancel_event.is_set()
                    and (not producer_finished or active_futures)
                ):
                    # Heartbeat: never go silent — report what is in flight.
                    if time.monotonic() - last_heartbeat >= 30.0:
                        last_heartbeat = time.monotonic()
                        app_logger.info(
                            "Job heartbeat | qualified=%d/%d | in_flight=%d | "
                            "queued=%d | discovered=%d",
                            qualified_count, goal_leads, len(active_futures),
                            raw_queue.qsize(), discovered_count,
                        )

                    # Submit tasks from raw_queue
                    while (
                        not raw_queue.empty()
                        and not cancel_event.is_set()
                        and len(active_futures) < self.concurrency * 2
                    ):
                        if shutdown_core.is_shutting_down():
                            stop_event.set()
                            break
                        try:
                            raw_lead = raw_queue.get_nowait()
                            if raw_lead is None:
                                producer_finished = True
                                break
                            if isinstance(raw_lead, dict) and raw_lead.get("__progress__"):
                                handle_progress(raw_lead)
                                continue
                            discovered_count += 1
                            app_logger.info(
                                "Lead submitted | name=%s | website=%s | in_flight=%d",
                                raw_lead.get("name", "Unknown"),
                                raw_lead.get("website") or "none",
                                len(active_futures) + 1,
                            )
                            fut = pool.submit(
                                self._process_candidate,
                                raw_lead=raw_lead,
                                agency_services=plan.get("agency_services", ""),
                                target_defects=target_defects,
                                cancel_event=cancel_event,
                            )
                            active_futures.add(fut)
                        except queue.Empty:
                            break

                    # Collect finished tasks
                    if active_futures:
                        done_futures, _ = concurrent.futures.wait(
                            active_futures,
                            timeout=0.1,
                            return_when=concurrent.futures.FIRST_COMPLETED,
                        )
                        done_futures = list(done_futures)
                    else:
                        # No work in flight: block briefly for the producer instead of
                        # spinning on a fixed sleep.
                        if shutdown_core.is_shutting_down() and not active_futures:
                            stop_event.set()
                            break
                        try:
                            raw_lead = raw_queue.get(timeout=0.1)
                            if raw_lead is None:
                                producer_finished = True
                            elif isinstance(raw_lead, dict) and raw_lead.get("__progress__"):
                                handle_progress(raw_lead)
                            else:
                                discovered_count += 1
                                app_logger.info(
                                    "Lead submitted | name=%s | website=%s | in_flight=%d",
                                    raw_lead.get("name", "Unknown"),
                                    raw_lead.get("website") or "none",
                                    len(active_futures) + 1,
                                )
                                fut = pool.submit(
                                    self._process_candidate,
                                    raw_lead=raw_lead,
                                    agency_services=plan.get("agency_services", ""),
                                    target_defects=target_defects,
                                    cancel_event=cancel_event,
                                )
                                active_futures.add(fut)
                        except queue.Empty:
                            pass
                        done_futures = []

                    for f in done_futures:
                        active_futures.remove(f)
                        try:
                            enriched_lead = f.result()

                            # Every processed lead lands in the all-leads CSV,
                            # qualified or not, before any filtering.
                            try:
                                all_exporter.append_lead(enriched_lead)
                            except Exception as csv_err:
                                app_logger.error(
                                    "All-leads CSV write failed for %s: %s",
                                    enriched_lead.get("name"), csv_err,
                                )

                            if only_qualified_redesigns and not enriched_lead.get("needs_redesign"):
                                skipped_count += 1
                                app_logger.info(
                                    "Lead skipped (passable design): %s | skipped_total=%d | qualified=%d/%d",
                                    enriched_lead.get("name"),
                                    skipped_count, qualified_count, goal_leads,
                                )
                                continue

                            exporter.append_lead(enriched_lead)
                            qualified_count += 1

                            if progress_callback:
                                progress_callback(enriched_lead)

                            yield enriched_lead

                            if qualified_count >= goal_leads:
                                stop_event.set()
                                return
                        except Exception as err:
                            app_logger.error("Lead processing error: %s", err)
        finally:
            # Always stop discovery, including when the consumer disconnects or
            # this generator is closed early. This releases a producer blocked on
            # queue backpressure instead of leaking a daemon thread.
            stop_event.set()
            producer_thread.join(timeout=2.0)

        if qualified_count < goal_leads and not cancel_event.is_set():
            app_logger.warning(
                "Target not reached: qualified %d/%d leads after processing %d "
                "candidates (%d skipped as passable design). Discovery returned "
                "no more leads — broaden the niche/location or raise MAX_LEADS. "
                "All processed leads (including skipped) are in %s",
                qualified_count, goal_leads, discovered_count, skipped_count,
                getattr(exporter, "all_leads_path", ""),
            )

    def run_to_csv(
        self,
        user_query: str,
        agency_services: Optional[str] = None,
        target_count: int = 10,
        only_qualified_redesigns: bool = True,
        csv_filename: Optional[str] = None,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """Runs the pipeline and returns (csv_path, collected_leads)."""
        collected = []
        exporter = CSVLeadExporter(filename=csv_filename)

        for lead in self.execute(
            user_query=user_query,
            agency_services=agency_services,
            target_count=target_count,
            only_qualified_redesigns=only_qualified_redesigns,
            csv_filename=os.path.basename(exporter.filepath),
        ):
            collected.append(lead)

        return str(exporter.filepath), collected