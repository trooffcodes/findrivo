"""
config.py
Centralized configuration manager for FindRivo SaaS & CLI pipeline.
"""

import os
from pathlib import Path
from typing import List
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("true", "1", "yes", "t")


def _path_env(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    path = Path(raw).expanduser() if raw else default
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    return path.resolve()


BASE_DIR = Path(__file__).resolve().parents[1]
LOG_DIR = _path_env("FINDRIVO_LOG_DIR", BASE_DIR / "logs")
EXPORT_DIR = _path_env("FINDRIVO_EXPORT_DIR", BASE_DIR / "exports")
PROGRESS_FILE = _path_env("PROGRESS_FILE", BASE_DIR / "discovery_progress.json")
RESULTS_DIR = _path_env("FINDRIVO_RESULTS_DIR", BASE_DIR / "data" / "jobs")


class Config:
    BASE_DIR: Path = BASE_DIR
    LOG_DIR: Path = LOG_DIR
    EXPORT_DIR: Path = EXPORT_DIR
    RESULTS_DIR: Path = RESULTS_DIR
    ENVIRONMENT: str = os.getenv("FINDRIVO_ENV", "development").strip().lower()
    IS_PRODUCTION: bool = ENVIRONMENT == "production"
    DEBUG: bool = _bool_env("DEBUG", False)
    PORT: int = int(os.getenv("FLASK_PORT", "2111"))

    # API security. In production DEBUG must be false and either an API token must
    # be configured or unauthenticated access must be explicitly enabled.
    API_TOKEN: str = os.getenv("FINDRIVO_API_TOKEN", "").strip()
    ALLOW_UNAUTHENTICATED: bool = _bool_env("ALLOW_UNAUTHENTICATED", DEBUG)
    MAX_ACTIVE_JOBS: int = max(1, int(os.getenv("MAX_ACTIVE_JOBS", "2")))
    RATE_LIMIT_REQUESTS: int = max(0, int(os.getenv("RATE_LIMIT_REQUESTS", "20")))
    RATE_LIMIT_WINDOW_SECONDS: int = max(1, int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60")))
    MAX_TARGET_COUNT: int = max(1, min(500, int(os.getenv("MAX_TARGET_COUNT", "100"))))
    STREAM_TTL_SECONDS: int = max(60, int(os.getenv("STREAM_TTL_SECONDS", "3600")))

    # Result persistence (lead snapshots survive page reloads / restarts).
    RESULTS_RETENTION_HOURS: int = max(1, int(os.getenv("RESULTS_RETENTION_HOURS", "24")))

    # Graceful shutdown (Ctrl+C): how long active jobs may finish before exit.
    SHUTDOWN_GRACE_SECONDS: float = max(1.0, float(os.getenv("SHUTDOWN_GRACE_SECONDS", "12")))

    # LLM API Keys
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GEMINI_API_KEY_2: str = os.getenv("GEMINI_API_KEY_2", "")
    GROQ_API_KEY_1: str = os.getenv("GROQ_API_KEY_1", "")
    GROQ_API_KEY_2: str = os.getenv("GROQ_API_KEY_2", "")
    GROQ_API_KEY_3: str = os.getenv("GROQ_API_KEY_3", "")

    # Model Identifiers
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    GROQ_FALLBACK_MODEL: str = os.getenv("GROQ_FALLBACK_MODEL", "openai/gpt-oss-120b")
    QWEN_VISION_MODEL: str = os.getenv("QWEN_VISION_MODEL", "qwen/qwen3.8-27b")

    # Rate Limiting & Cooldowns
    GEMINI_COOLDOWN_SECONDS: int = int(os.getenv("GEMINI_COOLDOWN_SECONDS", "60"))
    GROQ_KEY_COOLDOWN_SECONDS: int = int(os.getenv("GROQ_KEY_COOLDOWN_SECONDS", "60"))
    GROQ_REQUESTS_PER_MINUTE: int = int(os.getenv("GROQ_REQUESTS_PER_MINUTE", "25"))
    GROQ_REQUESTS_PER_DAY: int = int(os.getenv("GROQ_REQUESTS_PER_DAY", "1000"))

    # Pipeline Concurrency & Network Limits
    DEFAULT_LEAD_TARGET: int = int(os.getenv("DEFAULT_LEAD_TARGET", "10"))
    REQUEST_TIMEOUT: float = float(os.getenv("REQUEST_TIMEOUT", "8"))
    LINK_CHECK_TIMEOUT: float = float(os.getenv("LINK_CHECK_TIMEOUT", "3.5"))
    MAX_AUDIT_CONCURRENCY: int = int(os.getenv("MAX_AUDIT_CONCURRENCY", "4"))
    LLM_TIMEOUT_SECONDS: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "45"))

    # Discovery & Browser Engine
    HEADLESS: bool = _bool_env("HEADLESS", True)
    DISCOVERY_CONCURRENCY: int = int(os.getenv("CONCURRENCY", "2"))
    MAX_LEADS_PER_LOCATION: int = int(os.getenv("MAX_LEADS", "20"))
    # How long a single Maps feed may be scrolled for link harvesting, and how
    # many consecutive no-new-results scroll checks end the harvest early.
    DISCOVERY_SCROLL_SECONDS: float = max(10.0, float(os.getenv("DISCOVERY_SCROLL_SECONDS", "120")))
    DISCOVERY_SCROLL_STAGNANT: int = max(1, int(os.getenv("DISCOVERY_SCROLL_STAGNANT", "4")))
    RANDOM_DELAY_MIN: float = float(os.getenv("DELAY_MIN", "0.6"))
    RANDOM_DELAY_MAX: float = float(os.getenv("DELAY_MAX", "1.4"))
    BLOCK_HEAVY_RESOURCES: bool = True
    PLAYWRIGHT_NO_SANDBOX: bool = _bool_env("PLAYWRIGHT_NO_SANDBOX", False)
    RESUME_DISCOVERY: bool = _bool_env("RESUME_DISCOVERY", False)
    PROGRESS_FILE: str = str(PROGRESS_FILE)

    # A locked CSV (usually open in Excel) is retried this many times before
    # the row is preserved in a *_recovered.csv sidecar file instead.
    CSV_WRITE_RETRIES: int = max(1, int(os.getenv("CSV_WRITE_RETRIES", "4")))

    # Playwright browser fallback (hybrid extraction). Disabled by default so an
    # optional dependency cannot silently dominate every lead.
    BROWSER_FALLBACK_ENABLED: bool = _bool_env("BROWSER_FALLBACK_ENABLED", True)
    # Number of parallel Chromium instances serving browser fallback. More
    # workers means leads never queue for minutes behind a single browser.
    BROWSER_FALLBACK_WORKERS: int = max(1, int(os.getenv("BROWSER_FALLBACK_WORKERS", "3")))
    BROWSER_FALLBACK_TIMEOUT: float = max(
        5.0, float(os.getenv("BROWSER_FALLBACK_TIMEOUT", "15"))
    )
    BROWSER_FALLBACK_MAX_PER_JOB: int = max(
        0, int(os.getenv("BROWSER_FALLBACK_MAX_PER_JOB", "15"))
    )
    # A host is not retried via browser for this long after a failure.
    BROWSER_FALLBACK_RETRY_COOLDOWN: int = max(
        60, int(os.getenv("BROWSER_FALLBACK_RETRY_COOLDOWN", "900"))
    )
    # Lead must clear these thresholds to justify browser rendering.
    BROWSER_FALLBACK_MIN_RATING: float = float(os.getenv("BROWSER_FALLBACK_MIN_RATING", "3.5"))
    BROWSER_FALLBACK_MIN_REVIEWS: int = max(0, int(os.getenv("BROWSER_FALLBACK_MIN_REVIEWS", "10")))

    # Defaults
    DEFAULT_US_LOCATIONS: List[str] = ["Austin, TX", "Miami, FL", "Seattle, WA", "Chicago, IL"]
    DEFAULT_NICHES: List[str] = ["Dental Clinic", "Coffee Roaster", "HVAC Contractor", "Boutique Hotel"]

    @classmethod
    def get_groq_keys(cls) -> List[str]:
        """Returns non-empty Groq keys from configuration."""
        keys = [cls.GROQ_API_KEY_1, cls.GROQ_API_KEY_2, cls.GROQ_API_KEY_3]
        return [k.strip() for k in keys if k and k.strip()]

    @classmethod
    def get_gemini_keys(cls) -> List[str]:
        """Returns non-empty Gemini keys from configuration."""
        keys = [cls.GEMINI_API_KEY, cls.GEMINI_API_KEY_2]
        return [k.strip() for k in keys if k and k.strip()]

    @property
    def gemini_keys(self) -> List[str]:
        return self.get_gemini_keys()

    @property
    def groq_keys(self) -> List[str]:
        return self.get_groq_keys()


settings = Config()