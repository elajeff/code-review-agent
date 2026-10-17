"""Small, explicit configuration for the Gemini-backed first version."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str
    gemini_model: str = "gemini-3.8-flash"
    max_diff_chars: int = 40_000
    max_findings: int = 8
    max_context_chars: int = 12_000


def load_settings() -> Settings:
    # Load credentials from this tool installation, not from the repository being reviewed.
    # Target repositories are untrusted input and must not control provider configuration.
    app_root = Path(__file__).resolve().parents[1]
    load_dotenv(app_root / ".env", override=False)
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key or key == "put-your-key-here":
        raise ValueError(
            "GEMINI_API_KEY is not configured. Copy .env.example to .env and add a key from Google AI Studio."
        )
    return Settings(
        gemini_api_key=key,
        gemini_model=os.getenv("GEMINI_MODEL", "").strip() or "gemini-3.8-flash",
        max_diff_chars=_bounded_int("REVIEW_MAX_DIFF_CHARS", 40_000, 5_000, 100_000),
        max_findings=_bounded_int("REVIEW_MAX_FINDINGS", 8, 1, 8),
        max_context_chars=_bounded_int("REVIEW_MAX_CONTEXT_CHARS", 12_000, 1_000, 30_000),
    )


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value
