"""
platforms/base.py - Abstract base for all posting platforms.
Provides: retry with exponential backoff, rate limiting, dry-run guard.
This folder is self-contained — drop it into any project.
"""
import time
import logging
from dataclasses import dataclass, field
from typing import Optional
from abc import ABC, abstractmethod

import config


@dataclass
class PostResult:
    success:          bool
    platform:         str
    platform_post_id: str  = ""
    platform_url:     str  = ""
    error_message:    str  = ""


class BasePlatform(ABC):
    """
    All platforms inherit from this.
    Subclasses implement: validate_credentials(), post_text(), post_image()
    """

    def __init__(self, name: str):
        self.name    = name
        self.log     = logging.getLogger(f"platform.{name}")
        self._limits = config.RATE_LIMITS.get(name, {})
        self._last_call_time: float = 0.0

    # ── Abstract interface ────────────────────────────

    @abstractmethod
    def validate_credentials(self) -> bool:
        ...

    @abstractmethod
    def post_text(self, text: str, link: str = "") -> PostResult:
        ...

    @abstractmethod
    def post_image(self, text: str, image_path: str, link: str = "") -> PostResult:
        ...

    # ── Shared helpers ────────────────────────────────

    def send(self, text: str, image_path: str = "", link: str = "") -> PostResult:
        """
        Public entry point. Handles dry-run and delegates to
        post_image (if image_path given) or post_text.
        """
        if config.TEST_MODE:
            self.log.info(f"[TEST] Would post to {self.name}: {text[:80]}")
            return PostResult(success=True, platform=self.name,
                              platform_post_id="test_mode")

        if config.DRY_RUN:
            self.log.info(f"[DRY RUN] {self.name}: {text[:80]}")
            return PostResult(success=True, platform=self.name,
                              platform_post_id="dry_run")

        self._rate_limit()
        attempts = self._limits.get("retry_attempts", 3)
        backoff  = self._limits.get("backoff_base", 2.0)

        for attempt in range(1, attempts + 1):
            try:
                if image_path:
                    result = self.post_image(text, image_path, link)
                else:
                    result = self.post_text(text, link)

                if result.success:
                    return result

                self.log.warning(f"⚠️  Attempt {attempt} failed: {result.error_message}")
            except Exception as e:
                self.log.warning(f"⚠️  Attempt {attempt} exception: {e}")

            if attempt < attempts:
                wait = backoff ** attempt
                self.log.info(f"   Retrying in {wait:.1f}s...")
                time.sleep(wait)

        return PostResult(success=False, platform=self.name,
                          error_message="Max retries exceeded")

    def _rate_limit(self):
        """Enforce minimum gap between calls based on requests_per_hour."""
        rph = self._limits.get("requests_per_hour", 200)
        min_gap = 3600.0 / rph
        elapsed = time.time() - self._last_call_time
        if elapsed < min_gap:
            time.sleep(min_gap - elapsed)
        self._last_call_time = time.time()
