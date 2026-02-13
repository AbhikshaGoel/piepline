"""
parser.py - RSS feed fetcher.
Parallel fetching, deduplication by hash, age filtering.
"""
import re
import hashlib
import logging
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict

import config

log = logging.getLogger(__name__)


class RSSParser:

    def __init__(self):
        try:
            import feedparser
            self._fp = feedparser
            self._ok = True
        except ImportError:
            log.error("❌ feedparser not installed — pip install feedparser")
            self._ok = False

    # ── Public ────────────────────────────────────────

    def parse_feeds(self, urls: List[str]) -> List[Dict]:
        """Fetch all feeds in parallel, return unique articles."""
        if not self._ok:
            return []

        log.info(f"📡 Fetching {len(urls)} RSS feeds...")
        all_articles: List[Dict] = []

        with ThreadPoolExecutor(max_workers=config.PIPELINE["max_feed_workers"]) as ex:
            futures = {ex.submit(self._fetch, u): u for u in urls}
            for fut in as_completed(futures):
                try:
                    all_articles.extend(fut.result())
                except Exception as e:
                    log.error(f"❌ Feed failed {futures[fut][:60]}: {e}")

        # Dedup by content hash
        seen, unique = set(), []
        for art in all_articles:
            h = art["content_hash"]
            if h not in seen:
                seen.add(h)
                unique.append(art)

        removed = len(all_articles) - len(unique)
        if removed:
            log.info(f"🔍 Removed {removed} feed-level duplicates")

        log.info(f"✅ {len(unique)} unique articles fetched")
        return unique

    # ── Private ───────────────────────────────────────

    def _fetch(self, url: str) -> List[Dict]:
        try:
            feed = self._fp.parse(url)
            articles = []
            cutoff = datetime.now(timezone.utc) - timedelta(
                hours=config.PIPELINE.get("max_age_hours", 48)
            )

            for entry in feed.entries:
                title   = entry.get("title", "No Title").strip()
                link    = entry.get("link", "")
                summary = self._clean_html(
                    entry.get("summary", "") or entry.get("description", "")
                )[:500]

                # Age filter
                pub = self._parse_date(entry)
                if pub and pub < cutoff:
                    continue

                articles.append({
                    "title":        title,
                    "link":         link,
                    "summary":      summary,
                    "published_at": pub.isoformat() if pub else None,
                    "content_hash": self._hash(title, link),
                    "source_feed":  url,
                })

            log.debug(f"  ✓ {len(articles)} articles ← {url[:60]}")
            return articles

        except Exception as e:
            log.error(f"❌ Parse error {url[:60]}: {e}")
            return []

    @staticmethod
    def _hash(title: str, link: str) -> str:
        return hashlib.sha256(f"{title}{link}".encode()).hexdigest()

    @staticmethod
    def _parse_date(entry) -> datetime | None:
        try:
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        except Exception:
            pass
        return None

    @staticmethod
    def _clean_html(text: str) -> str:
        text = re.sub(r"<[^>]+>", "", text)
        return " ".join(text.split())
