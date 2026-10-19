import base64
import time
from typing import Any, List, Optional, Union
from io import BytesIO
from PIL import Image
from dotenv import load_dotenv

# API SDKs
from google import genai
from google.genai import types as gemini_types
from groq import Groq, RateLimitError, APIError

from core.logger import app_logger, perf_logger, track_performance
from config import Config

load_dotenv()


class GroqKeyManager:
    """Manages round-robin rotation and basic rate-limit tracking for Groq keys."""

    def __init__(self, keys: List[str]):
        self.keys = [k for k in keys if k]
        if not self.keys:
            raise ValueError("At least one Groq API key must be provided.")
        self.current_index = 0
        self.clients = [Groq(api_key=key) for key in self.keys]

    def get_client(self) -> Groq:
        return self.clients[self.current_index]

    def rotate_key(self) -> Groq:
        self.current_index = (self.current_index + 1) % len(self.clients)
        app_logger.info("Rotated to Groq API Key index: %d", self.current_index)
        return self.clients[self.current_index]


class LLMClient:
    """
    Unified client supporting:
    - Primary: Gemini with Google Grounding tools.
    - Fallback: Groq with browser search tool and rotating keys.
    - Vision: Qwen multimodal image + text support.
    """

    def __init__(self):
        # Gemini setup
        self.gemini_api_key = Config.GEMINI_API_KEY
        if self.gemini_api_key:
            self.gemini_client = genai.Client(api_key=self.gemini_api_key)
        else:
            self.gemini_client = None
            app_logger.warning("GEMINI_API_KEY not found in environment.")

        # Groq setup
        groq_keys = Config.get_groq_keys()
        if groq_keys:
            self.groq_manager: Optional[GroqKeyManager] = GroqKeyManager(groq_keys)
        else:
            self.groq_manager = None
            app_logger.warning("No GROQ API keys configured.")

        self.gemini_model_id = Config.GEMINI_MODEL
        self.groq_fallback_model = Config.GROQ_FALLBACK_MODEL
        self.qwen_vision_model = Config.QWEN_VISION_MODEL

    # ==========================================
    # Gemini Execution
    # ==========================================

    @track_performance("llm_call_gemini")
    def call_gemini(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        system_prompt: Optional[str] = None,
        use_google_search: bool = False,
        use_url_context: bool = False,
        use_google_maps: bool = False,
        temperature: float = 0.7,
    ) -> str:
        """Calls Gemini model with grounding tool validation."""
        if not self.gemini_client:
            raise RuntimeError("Gemini client is uninitialized. Provide GEMINI_API_KEY.")

        effective_system = system_instruction or system_prompt

        # Tool validation rule enforcement
        if use_google_maps and use_url_context:
            raise ValueError(
                "Cannot use google_maps grounding with url_context in the same request."
            )

        tools: List[Any] = []
        if use_google_maps:
            tools.append(gemini_types.Tool(google_maps=gemini_types.GoogleMaps()))
        else:
            if use_google_search:
                tools.append(gemini_types.Tool(google_search=gemini_types.GoogleSearch()))
            if use_url_context:
                tools.append(gemini_types.Tool(url_context=gemini_types.UrlContext()))

        config = gemini_types.GenerateContentConfig(
            temperature=temperature,
            system_instruction=effective_system,
            tools=tools if tools else None,
        )

        response = self.gemini_client.models.generate_content(
            model=self.gemini_model_id,
            contents=prompt,
            config=config,
        )

        usage = getattr(response, "usage_metadata", None)
        in_tokens = getattr(usage, "prompt_token_count", 0) if usage else 0
        out_tokens = getattr(usage, "candidates_token_count", 0) if usage else 0

        perf_logger.info(
            "Gemini Token Usage | Model: %s | InTokens: %d | OutTokens: %d",
            self.gemini_model_id,
            in_tokens,
            out_tokens,
        )
        return response.text or ""

    # ==========================================
    # Groq Fallback & Search Execution
    # ==========================================

    @track_performance("llm_call_groq")
    def call_groq(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        system_prompt: Optional[str] = None,
        use_browser_search: bool = False,
        temperature: float = 0.5,
        max_retries: int = 3,
    ) -> str:
        """Executes inference on Groq with automated key rotation across retries."""
        if not self.groq_manager:
            raise RuntimeError("Groq key manager is uninitialized. Provide at least one GROQ_API_KEY.")

        effective_system = system_instruction or system_prompt
        messages = []
        if effective_system:
            messages.append({"role": "system", "content": effective_system})
        messages.append({"role": "user", "content": prompt})

        tools = [{"type": "browser_search"}] if use_browser_search else None
        attempts = 0

        while attempts < max_retries:
            client = self.groq_manager.get_client()
            try:
                kwargs: dict = {
                    "model": self.groq_fallback_model,
                    "messages": messages,
                    "temperature": temperature,
                }
                if tools:
                    kwargs["tools"] = tools

                response = client.chat.completions.create(**kwargs)
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
                app_logger.warning(
                    "Groq Rate Limit on Key %d: %s. Rotating key...",
                    self.groq_manager.current_index,
                    rle,
                )
                self.groq_manager.rotate_key()
                time.sleep(1.0)

            except APIError as api_err:
                attempts += 1
                app_logger.error(
                    "Groq API error on Key %d: %s. Rotating key...",
                    self.groq_manager.current_index,
                    api_err,
                )
                self.groq_manager.rotate_key()
                time.sleep(1.0)

        raise RuntimeError(f"All Groq API keys exhausted or rate-limited after {max_retries} retries.")

    @track_performance("llm_groq_fallback")
    def call_groq_with_fallback(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        system_instruction: Optional[str] = None,
        **kwargs,
    ) -> str:
        """Convenience method matching gateway naming conventions across downstream tasks."""
        return self.call_groq(
            prompt=prompt,
            system_instruction=system_instruction or system_prompt,
            **kwargs,
        )

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
        """Attempts execution on Gemini. Falls back to Groq if rate-limited or unavailable."""
        effective_system = system_instruction or system_prompt
        try:
            return self.call_gemini(
                prompt=prompt,
                system_instruction=effective_system,
                use_google_search=use_search,
                use_google_maps=use_maps,
            )
        except Exception as e:
            app_logger.warning("Primary engine (Gemini) failed: %s. Initiating Groq fallback.", e)
            return self.call_groq(
                prompt=prompt,
                system_instruction=effective_system,
                use_browser_search=use_search,
            )

    # ==========================================
    # Multimodal Vision (Qwen)
    # ==========================================

    @track_performance("llm_call_qwen_vision")
    def call_qwen_vision(
        self,
        image_input: Union[str, bytes, Image.Image],
        prompt: Optional[str] = "Analyze this image and describe defects or features.",
        system_instruction: Optional[str] = None,
        system_prompt: Optional[str] = None,
        max_retries: int = 3,
    ) -> str:
        """Multimodal image + text inference using rotating Groq API instances."""
        if not self.groq_manager:
            raise RuntimeError("Groq key manager is uninitialized for vision model.")

        base64_img = self._encode_image_to_base64(image_input)

        content: List[dict] = []
        if prompt:
            content.append({"type": "text", "text": prompt})

        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{base64_img}"
            }
        })

        effective_system = system_instruction or system_prompt
        messages = []
        if effective_system:
            messages.append({"role": "system", "content": effective_system})
        messages.append({"role": "user", "content": content})

        attempts = 0
        while attempts < max_retries:
            client = self.groq_manager.get_client()
            try:
                response = client.chat.completions.create(
                    model=self.qwen_vision_model,
                    messages=messages,
                    temperature=0.2,
                )
                in_tokens = response.usage.prompt_tokens if response.usage else 0
                out_tokens = response.usage.completion_tokens if response.usage else 0

                perf_logger.info(
                    "Qwen Vision Usage | Model: %s | InTokens: %d | OutTokens: %d",
                    self.qwen_vision_model,
                    in_tokens,
                    out_tokens,
                )
                return response.choices[0].message.content or ""

            except (RateLimitError, APIError) as err:
                attempts += 1
                app_logger.warning(
                    "Vision request error on Key %d: %s. Rotating...",
                    self.groq_manager.current_index,
                    err,
                )
                self.groq_manager.rotate_key()
                time.sleep(1.0)

        raise RuntimeError(f"Vision call to {self.qwen_vision_model} failed after {max_retries} attempts.")

    # ==========================================
    # Helper Methods
    # ==========================================

    @staticmethod
    def _encode_image_to_base64(image_input: Union[str, bytes, Image.Image]) -> str:
        """Normalizes and compresses images to base64 JPEG format."""
        if isinstance(image_input, str):
            with open(image_input, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")

        if isinstance(image_input, bytes):
            return base64.b64encode(image_input).decode("utf-8")

        if isinstance(image_input, Image.Image):
            buffered = BytesIO()
            img = image_input.convert("RGB")
            img.save(buffered, format="JPEG", quality=85)
            return base64.b64encode(buffered.getvalue()).decode("utf-8")

        raise TypeError("Unsupported image input type. Use str path, raw bytes, or PIL Image.")


# Backward-compatible alias for existing imports in methods
LLMGateway = LLMClient