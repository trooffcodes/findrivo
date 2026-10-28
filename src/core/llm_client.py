"""
core/llm_client.py
Unified, fault-tolerant LLM Client with Circuit Breaker, automatic key rotation,
Google Grounding, and deterministic heuristic fallbacks.
"""

import base64
import json
import threading
import time
from io import BytesIO
from typing import Any, Dict, List, Optional, Union
from PIL import Image

from google import genai
from google.genai import types as gemini_types
from google.genai import errors as genai_errors
from groq import Groq, RateLimitError, APIError

from config import Config
from core.circuit_breaker import CircuitBreaker
from core.logger import app_logger, perf_logger, track_performance


class GeminiKeyManager:
    """Round-robin rotation and per-key cooldowns for Gemini API keys.

    Rate limits (HTTP 429) and transient overload (503) rotate to the next key
    instead of tripping the provider-wide circuit breaker, so one exhausted key
    never takes the primary provider down while the other still has quota.
    """

    def __init__(self, keys: List[str], timeout_ms: int):
        self.keys = [k for k in keys if k]
        if not self.keys:
            raise ValueError("At least one Gemini API key must be provided.")
        self.clients = []
        for key in self.keys:
            try:
                self.clients.append(
                    genai.Client(
                        api_key=key,
                        http_options=gemini_types.HttpOptions(timeout=timeout_ms),
                    )
                )
            except Exception as e:
                app_logger.error("Failed to initialize Gemini client for a key: %s", e)
        if not self.clients:
            raise ValueError("No Gemini client could be initialized.")
        self.current_index = 0
        self._key_cooldowns: Dict[int, float] = {}
        self._lock = threading.Lock()

    def get_client(self):
        """Returns the next available, non-cooldown Gemini client."""
        with self._lock:
            now = time.time()
            for _ in range(len(self.clients)):
                idx = self.current_index
                cooldown_expiry = self._key_cooldowns.get(idx, 0.0)
                if now >= cooldown_expiry:
                    return self.clients[idx]
                self.current_index = (self.current_index + 1) % len(self.clients)

            # All keys are in cooldown; return current client as best effort
            return self.clients[self.current_index]

    def mark_rate_limited(self, cooldown_seconds: float = 60.0):
        """Marks the current key index as rate-limited and advances."""
        with self._lock:
            now = time.time()
            self._key_cooldowns[self.current_index] = now + cooldown_seconds
            app_logger.warning(
                "Gemini key index %d marked in cooldown for %.1fs.",
                self.current_index,
                cooldown_seconds,
            )
            self.current_index = (self.current_index + 1) % len(self.clients)


class GroqKeyManager:
    """Manages round-robin rotation and per-key rate-limit cooldowns."""

    def __init__(self, keys: List[str]):
        self.keys = [k for k in keys if k]
        if not self.keys:
            raise ValueError("At least one Groq API key must be provided.")
        self.current_index = 0
        self.clients = [
            Groq(
                api_key=key,
                timeout=Config.LLM_TIMEOUT_SECONDS,
                max_retries=0,
            )
            for key in self.keys
        ]
        self._key_cooldowns: Dict[int, float] = {}
        self._lock = threading.Lock()

    def get_client(self) -> Groq:
        """Returns the next available, non-cooldown Groq client."""
        with self._lock:
            now = time.time()
            for _ in range(len(self.clients)):
                idx = self.current_index
                cooldown_expiry = self._key_cooldowns.get(idx, 0.0)
                if now >= cooldown_expiry:
                    return self.clients[idx]
                self.current_index = (self.current_index + 1) % len(self.clients)

            # All keys are in cooldown; return current client as best effort
            return self.clients[self.current_index]

    def mark_rate_limited(self, cooldown_seconds: float = 60.0):
        """Marks the current key index as rate-limited and advances."""
        with self._lock:
            now = time.time()
            self._key_cooldowns[self.current_index] = now + cooldown_seconds
            app_logger.warning(
                "Groq key index %d marked in cooldown for %.1fs.",
                self.current_index,
                cooldown_seconds,
            )
            self.current_index = (self.current_index + 1) % len(self.clients)


class LLMClient:
    """
    Unified LLM Gateway:
      - Primary: Gemini with Google Grounding & Circuit Breaker.
      - Secondary: Groq with multi-key rotation and cooldown tracking.
      - Multimodal: Qwen Vision via Groq.
      - Tertiary: Deterministic heuristic fallback generators.
    """

    def __init__(self):
        self.gemini_circuit = CircuitBreaker("Gemini", default_cooldown=Config.GEMINI_COOLDOWN_SECONDS)
        self.gemini_model_id = Config.GEMINI_MODEL
        self.groq_fallback_model = Config.GROQ_FALLBACK_MODEL
        self.qwen_vision_model = Config.QWEN_VISION_MODEL

        gemini_keys = Config.get_gemini_keys()
        self.gemini_manager: Optional[GeminiKeyManager] = None
        if gemini_keys:
            try:
                self.gemini_manager = GeminiKeyManager(
                    gemini_keys, timeout_ms=int(Config.LLM_TIMEOUT_SECONDS * 1000)
                )
            except ValueError as e:
                app_logger.error("Gemini key manager unavailable: %s", e)

        groq_keys = Config.get_groq_keys()
        self.groq_manager: Optional[GroqKeyManager] = GroqKeyManager(groq_keys) if groq_keys else None

    # ==========================================
    # Gemini Execution with Circuit Breaker
    # ==========================================

    @track_performance("llm_call_gemini")
    def call_gemini(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        use_google_search: bool = False,
        use_google_maps: bool = False,
        temperature: float = 0.7,
        _attempt: int = 0,
    ) -> str:
        if not self.gemini_manager:
            raise RuntimeError("Gemini client is uninitialized. Configure GEMINI_API_KEY.")

        if not self.gemini_circuit.is_available:
            raise RuntimeError(f"Gemini Circuit Breaker is OPEN. Bypassing {self.gemini_model_id}.")

        client = self.gemini_manager.get_client()

        tools: List[Any] = []
        try:
            if use_google_maps and hasattr(gemini_types, "GoogleMaps"):
                tools.append(gemini_types.Tool(google_maps=gemini_types.GoogleMaps()))
            elif use_google_search and hasattr(gemini_types, "GoogleSearch"):
                tools.append(gemini_types.Tool(google_search=gemini_types.GoogleSearch()))
        except Exception as e:
            app_logger.debug("Optional Gemini tool registration omitted: %s", e)

        config = gemini_types.GenerateContentConfig(
            temperature=temperature,
            system_instruction=system_instruction,
            tools=tools if tools else None,
        )

        try:
            response = client.models.generate_content(
                model=self.gemini_model_id,
                contents=prompt,
                config=config,
            )
            self.gemini_circuit.record_success()

            usage = getattr(response, "usage_metadata", None)
            in_tokens = getattr(usage, "prompt_token_count", 0) if usage else 0
            out_tokens = getattr(usage, "candidates_token_count", 0) if usage else 0

            perf_logger.info(
                "Gemini Token Usage | Model: %s | InTokens: %d | OutTokens: %d | KeyIndex: %d",
                self.gemini_model_id,
                in_tokens,
                out_tokens,
                self.gemini_manager.current_index,
            )
            return response.text or ""

        except genai_errors.APIError as err:
            status = getattr(err, "code", None) or getattr(err, "status_code", None)
            if status == 429:
                # Quota exhausted on this key: rotate and retry on the next one.
                self.gemini_manager.mark_rate_limited(cooldown_seconds=Config.GEMINI_COOLDOWN_SECONDS)
                if _attempt + 1 < len(self.gemini_manager.clients):
                    app_logger.warning(
                        "Gemini key rate-limited (429). Rotating to next key (attempt %d).",
                        _attempt + 1,
                    )
                    return self.call_gemini(
                        prompt=prompt,
                        system_instruction=system_instruction,
                        use_google_search=use_google_search,
                        use_google_maps=use_google_maps,
                        temperature=temperature,
                        _attempt=_attempt + 1,
                    )
                # All keys exhausted: let the circuit breaker see it.
                self.gemini_circuit.record_failure(err)
                raise
            if status == 503:
                # Transient model overload: short cooldown, rotate, retry once.
                self.gemini_manager.mark_rate_limited(cooldown_seconds=15.0)
                if _attempt + 1 < len(self.gemini_manager.clients):
                    app_logger.warning(
                        "Gemini overloaded (503). Rotating to next key (attempt %d).",
                        _attempt + 1,
                    )
                    return self.call_gemini(
                        prompt=prompt,
                        system_instruction=system_instruction,
                        use_google_search=use_google_search,
                        use_google_maps=use_google_maps,
                        temperature=temperature,
                        _attempt=_attempt + 1,
                    )
                self.gemini_circuit.record_failure(err)
                raise

            self.gemini_circuit.record_failure(err)
            raise

        except Exception as err:
            self.gemini_circuit.record_failure(err)
            raise

    # ==========================================
    # Groq Fallback & Multi-Key Execution
    # ==========================================

    @track_performance("llm_call_groq")
    def call_groq(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        temperature: float = 0.4,
        max_retries: int = 3,
    ) -> str:
        if not self.groq_manager:
            raise RuntimeError("Groq key manager is uninitialized. Configure at least one GROQ_API_KEY.")

        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})

        attempts = 0
        last_error = None

        while attempts < max_retries:
            client = self.groq_manager.get_client()
            try:
                response = client.chat.completions.create(
                    model=self.groq_fallback_model,
                    messages=messages,
                    temperature=temperature,
                )
                in_tokens = response.usage.prompt_tokens if response.usage else 0
                out_tokens = response.usage.completion_tokens if response.usage else 0

                perf_logger.info(
                    "Groq Token Usage | Model: %s | InTokens: %d | OutTokens: %d | KeyIndex: %d",
                    self.groq_fallback_model,
                    in_tokens,
                    out_tokens,
                    self.groq_manager.current_index,
                )
                return response.choices[0].message.content or ""

            except RateLimitError as rle:
                attempts += 1
                last_error = rle
                self.groq_manager.mark_rate_limited(cooldown_seconds=Config.GROQ_KEY_COOLDOWN_SECONDS)
                time.sleep(0.5)

            except APIError as api_err:
                attempts += 1
                last_error = api_err
                app_logger.warning("Groq API error on Key %d: %s. Rotating...", self.groq_manager.current_index, api_err)
                self.groq_manager.mark_rate_limited(cooldown_seconds=15.0)
                time.sleep(0.5)

        raise RuntimeError(f"All Groq keys exhausted or failed after {max_retries} attempts: {last_error}")

    def call_groq_with_fallback(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        system_instruction: Optional[str] = None,
        **kwargs,
    ) -> str:
        """Unified method for critic and extraction tasks with full failover."""
        effective_sys = system_instruction or system_prompt
        return self.query(prompt=prompt, system_instruction=effective_sys, **kwargs)

    # ==========================================
    # Unified Primary-to-Fallback Query
    # ==========================================

    @track_performance("llm_unified_query")
    def query(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        system_prompt: Optional[str] = None,
        use_search: bool = False,
        use_maps: bool = False,
    ) -> str:
        """Executes query on Gemini, falls back to Groq, and handles all failures gracefully.

        Each provider call is wrapped in a HARD wall-clock deadline. SDK-level
        timeouts have proven unreliable (observed 264s Groq / 277s Gemini calls
        against a 45s config), and one hung call stalls the whole worker pool —
        so here the call is simply abandoned at the deadline and the next
        provider (or the deterministic fallback) takes over.
        """
        effective_sys = system_instruction or system_prompt
        deadline = max(5.0, float(Config.LLM_TIMEOUT_SECONDS))

        # 1. Try Gemini if healthy
        if self.gemini_manager and self.gemini_circuit.is_available:
            try:
                return self._run_with_deadline(
                    self.call_gemini,
                    deadline,
                    "gemini",
                    prompt=prompt,
                    system_instruction=effective_sys,
                    use_google_search=use_search,
                    use_google_maps=use_maps,
                )
            except TimeoutError as e:
                app_logger.warning("Gemini hard deadline (%.0fs) hit: %s", deadline, e)
                self.gemini_circuit.record_failure(e)
            except Exception as e:
                app_logger.warning("Gemini primary provider failed (%s). Triggering Groq fallback.", e)

        # 2. Try Groq
        if self.groq_manager:
            try:
                return self._run_with_deadline(
                    self.call_groq,
                    deadline,
                    "groq",
                    prompt=prompt,
                    system_instruction=effective_sys,
                )
            except Exception as e:
                app_logger.error("Groq fallback provider failed: %s", e)

        # 3. Tertiary Deterministic Fallback
        app_logger.warning("All LLM providers unavailable. Employing deterministic fallback response.")
        return self._heuristic_fallback(prompt, effective_sys)

    @staticmethod
    def _run_with_deadline(fn, timeout: float, op_name: str, *args, **kwargs) -> str:
        """Run `fn` in a helper thread and abandon it past `timeout` seconds.

        The abandoned thread may linger in the SDK, but the caller returns
        immediately so a single hung HTTP request can never freeze a lead (or
        the whole pipeline) for minutes.
        """
        outcome: Dict[str, Any] = {}

        def _target() -> None:
            try:
                outcome["value"] = fn(*args, **kwargs)
            except BaseException as err:  # noqa: BLE001 - relayed to the caller
                outcome["error"] = err

        worker = threading.Thread(target=_target, daemon=True, name=f"llm-{op_name}-deadline")
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            raise TimeoutError(f"{op_name} call exceeded {timeout:.0f}s hard deadline")
        if "error" in outcome:
            raise outcome["error"]
        return outcome.get("value", "")

    @staticmethod
    def _heuristic_fallback(prompt: str, system_prompt: Optional[str]) -> str:
        """Generates schema-compliant JSON if all LLMs are unreachable."""
        sys_str = (system_prompt or "").lower()

        if "planner" in sys_str:
            return json.dumps({
                "industry": "Local Services",
                "location": "Target Market",
                "target_defects": ["missing_mobile_viewport", "slow_speed", "no_ssl"],
                "must_have_contacts": ["email", "phone"],
                "search_queries": ["Local Services in Target Market"],
                "min_relevance_score": 70,
            })

        if "critic" in sys_str:
            return json.dumps({
                "viability_score": 75,
                "confidence_score": 80,
                "design_verdict": "outdated",
                "primary_defects": ["Non-responsive layout elements", "Slow mobile response time"],
                "rejection_reason": None,
            })

        if "personalization" in sys_str:
            return json.dumps({
                "company_name": "Prospective Client",
                "one_liner": "Established business with established local customer base.",
                "core_value_proposition": "Quality client-focused services.",
                "target_market": "Local Community",
                "recent_developments_or_focus": ["Digital service expansion"],
                "personalization_hooks": ["Booking form breaks on mobile (submit button sits outside the viewport), costing mobile leads"],
                "suggested_outreach_angle": "Modern web speed and conversion optimization",
                "confidence_score": 0.8,
            })

        if "pitch" in sys_str:
            return json.dumps({
                "hook": "We noticed your site experiences mobile conversion friction and slow response times, reducing direct client bookings.",
                "service_angle": "Mobile Redesign & Speed Optimization",
            })

        return "{}"

    # ==========================================
    # Multimodal Vision (Qwen)
    # ==========================================

    @track_performance("llm_call_qwen_vision")
    def call_qwen_vision(
        self,
        image_input: Union[str, bytes, Image.Image],
        prompt: Optional[str] = "Analyze this website screenshot for UX and design flaws.",
        system_instruction: Optional[str] = None,
        max_retries: int = 2,
    ) -> str:
        if not self.groq_manager:
            raise RuntimeError("Groq key manager required for Vision model.")

        base64_img = self._encode_image_to_base64(image_input)
        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})

        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": prompt or ""},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_img}"}},
            ],
        })

        last_error = None
        for _ in range(max(1, max_retries)):
            client = self.groq_manager.get_client()
            try:
                response = client.chat.completions.create(
                    model=self.qwen_vision_model,
                    messages=messages,
                    temperature=0.2,
                )
                return response.choices[0].message.content or ""
            except RateLimitError as err:
                last_error = err
                self.groq_manager.mark_rate_limited(
                    cooldown_seconds=Config.GROQ_KEY_COOLDOWN_SECONDS
                )
                time.sleep(0.5)
            except APIError as err:
                last_error = err
                self.groq_manager.mark_rate_limited(cooldown_seconds=15.0)
                time.sleep(0.5)
            except Exception as err:
                last_error = err
                break

        app_logger.error(
            "Vision request to %s failed: %s",
            self.qwen_vision_model,
            last_error,
        )
        return "{}"

    @staticmethod
    def _encode_image_to_base64(image_input: Union[str, bytes, Image.Image]) -> str:
        if isinstance(image_input, str):
            with open(image_input, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
        if isinstance(image_input, bytes):
            return base64.b64encode(image_input).decode("utf-8")
        if isinstance(image_input, Image.Image):
            buf = BytesIO()
            image_input.convert("RGB").save(buf, format="JPEG", quality=85)
            return base64.b64encode(buf.getvalue()).decode("utf-8")
        raise TypeError("Unsupported image format. Provide file path, raw bytes, or PIL Image.")


LLMGateway = LLMClient