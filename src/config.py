import os
from typing import List
from dotenv import load_dotenv

load_dotenv()


class Config:
    DEBUG: bool = os.getenv("DEBUG", "true").lower() in ("true", "1", "yes", "t")

    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GROQ_API_KEY_1: str = os.getenv("GROQ_API_KEY_1", "")
    GROQ_API_KEY_2: str = os.getenv("GROQ_API_KEY_2", "")
    GROQ_API_KEY_3: str = os.getenv("GROQ_API_KEY_3", "")

    # Model Identifiers
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    GROQ_FALLBACK_MODEL: str = os.getenv("GROQ_FALLBACK_MODEL", "openai/gpt-oss-120b")
    QWEN_VISION_MODEL: str = os.getenv("QWEN_VISION_MODEL", "qwen/qwen3.8-27b")

    # Groq Quotas & Limits (oss-120b)
    GROQ_REQUESTS_PER_MINUTE: int = int(os.getenv("GROQ_REQUESTS_PER_MINUTE", "30"))
    GROQ_REQUESTS_PER_DAY: int = int(os.getenv("GROQ_REQUESTS_PER_DAY", "1000"))
    GROQ_TOKENS_PER_MINUTE: int = int(os.getenv("GROQ_TOKENS_PER_MINUTE", "8000"))
    GROQ_TOKENS_PER_DAY: int = int(os.getenv("GROQ_TOKENS_PER_DAY", "200000"))

    # Pipeline Defaults
    DEFAULT_LEAD_TARGET: int = int(os.getenv("DEFAULT_LEAD_TARGET", "100"))
    REQUEST_TIMEOUT: int = int(os.getenv("REQUEST_TIMEOUT", "10"))
    MAX_AUDIT_CONCURRENCY: int = int(os.getenv("MAX_AUDIT_CONCURRENCY", "10"))

    @classmethod
    def get_groq_keys(cls) -> List[str]:
        """Returns non-empty Groq keys from class attributes."""
        return [k for k in (cls.GROQ_API_KEY_1, cls.GROQ_API_KEY_2, cls.GROQ_API_KEY_3) if k]

    @property
    def groq_keys(self) -> List[str]:
        """Returns non-empty Groq keys on instance access."""
        return self.get_groq_keys()


# Ready-to-use instance
settings = Config()