"""Runtime settings, read from environment variables (Cloud Run injects these)."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    env: str = "development"
    mongo_uri: str = ""
    mongo_db: str = "dietary_insight"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-flash-latest"
    ai_timeout_s: float = 75.0
    # Hard ceiling on live Gemini calls per UTC day, across all partners.
    # Protects you from a surprise bill while you're on free credits.
    daily_ai_call_cap: int = 500
    max_photo_bytes: int = 8 * 1024 * 1024
    analysis_version: str = "2026-10"

    @property
    def is_production(self) -> bool:
        return self.env == "production"


def load_settings() -> Settings:
    s = Settings(
        env=os.getenv("ENV", "development"),
        mongo_uri=os.getenv("MONGO_URI", ""),
        mongo_db=os.getenv("MONGO_DB", "dietary_insight"),
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-flash-latest"),
        ai_timeout_s=float(os.getenv("AI_TIMEOUT_S", "75")),
        daily_ai_call_cap=int(os.getenv("DAILY_AI_CALL_CAP", "500")),
        max_photo_bytes=int(os.getenv("MAX_PHOTO_BYTES", str(8 * 1024 * 1024))),
        analysis_version=os.getenv("ANALYSIS_VERSION", "2026-10"),
    )
    if s.is_production:
        missing = [n for n, v in (("MONGO_URI", s.mongo_uri), ("GEMINI_API_KEY", s.gemini_api_key)) if not v]
        if missing:
            raise RuntimeError(f"Missing required settings in production: {', '.join(missing)}")
    return s
