"""Central configuration. Secrets are read from the environment / .env only.

Nothing here hard-codes a key, so committing this file to a public repo is safe.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env if present (git-ignored). Silent if it does not exist.
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

UPLOAD_DIR = BASE_DIR / "uploads"
EXPORT_DIR = BASE_DIR / "exports"
UPLOAD_DIR.mkdir(exist_ok=True)
EXPORT_DIR.mkdir(exist_ok=True)


@dataclass
class Settings:
    finnhub_api_key: str = field(default_factory=lambda: os.getenv("FINNHUB_API_KEY", "").strip())
    fred_api_key: str = field(default_factory=lambda: os.getenv("FRED_API_KEY", "").strip())
    eodhd_api_key: str = field(default_factory=lambda: os.getenv("EODHD_API_KEY", "").strip())
    allowed_origins: str = field(default_factory=lambda: os.getenv(
        "ALLOWED_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000").strip())
    host: str = field(default_factory=lambda: os.getenv("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.getenv("PORT", "8000")))
    max_upload_mb: int = field(default_factory=lambda: int(os.getenv("MAX_UPLOAD_MB", "50")))
    max_pdf_pages: int = field(default_factory=lambda: int(os.getenv("MAX_PDF_PAGES", "500")))
    max_text_pages: int = field(default_factory=lambda: int(os.getenv("MAX_TEXT_PAGES", "80")))
    max_ocr_pages: int = field(default_factory=lambda: int(os.getenv("MAX_OCR_PAGES", "40")))
    max_browser_text_mb: int = field(default_factory=lambda: int(os.getenv("MAX_BROWSER_TEXT_MB", "20")))

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip().rstrip("/") for origin in self.allowed_origins.split(",") if origin.strip()]

    def status(self) -> dict[str, bool]:
        """Which optional keys are present (never exposes the value)."""
        return {
            "finnhub": bool(self.finnhub_api_key),
            "fred": bool(self.fred_api_key),
            "eodhd": bool(self.eodhd_api_key),
        }


settings = Settings()
