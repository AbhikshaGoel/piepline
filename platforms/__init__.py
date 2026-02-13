"""
platforms/__init__.py
Factory: get list of enabled platform instances.
Drop this folder into any project — only needs config.py.
"""
from __future__ import annotations
from typing import List
from platforms.base import BasePlatform
import config


def get_enabled_platforms() -> List[BasePlatform]:
    """Instantiate and return all enabled platforms."""
    platforms = []
    for name in config.ENABLED_PLATFORMS:
        try:
            platform = _build(name)
            if platform:
                platforms.append(platform)
        except Exception as e:
            import logging
            logging.getLogger("platforms").error(f"❌ Failed to init {name}: {e}")
    return platforms


def _build(name: str) -> BasePlatform | None:
    if name == "facebook":
        from platforms.facebook import FacebookPlatform
        return FacebookPlatform()
    if name == "instagram":
        from platforms.instagram import InstagramPlatform
        return InstagramPlatform()
    if name == "twitter":
        from platforms.twitter import TwitterPlatform
        return TwitterPlatform()
    if name == "youtube":
        from platforms.youtube import YouTubePlatform
        return YouTubePlatform()
    if name == "telegram":
        from platforms.telegram import TelegramPlatform
        return TelegramPlatform()
    return None
