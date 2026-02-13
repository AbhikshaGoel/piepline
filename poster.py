"""
poster.py - Orchestrates the full posting flow:
  1. Telegram approval (per article, with keyboard)
  2. On approve → post to all enabled platforms
  3. Log results to DB
"""
import logging
from typing import List, Dict
from pathlib import Path

import config
import db

log = logging.getLogger("poster")


class Poster:
    """
    Takes a list of selected articles → gets Telegram approval → posts everywhere.
    """

    def __init__(self):
        self._platforms  = []
        self._tg_approval = None
        self._ready      = False

    def start(self) -> bool:
        """Initialize platforms and Telegram bot."""
        from platforms import get_enabled_platforms
        self._platforms = get_enabled_platforms()

        if "telegram" in config.ENABLED_PLATFORMS:
            from platforms.telegram import TelegramApproval
            self._tg_approval = TelegramApproval()

        self._ready = True
        enabled = [p.name for p in self._platforms]
        log.info(f"✅ Poster ready. Platforms: {enabled or ['(none)']}")
        return True

    def stop(self):
        self._ready = False

    def post_articles(self, articles: List[Dict]) -> Dict:
        """
        Main entry point called by pipeline.
        Returns summary of what was approved/posted/skipped.
        """
        if not self._ready:
            self.start()

        summary = {"approved": 0, "skipped": 0, "posted": 0, "errors": 0}

        for article in articles:
            # ── Telegram Approval ──────────────────────
            approved = True
            if self._tg_approval:
                approved = self._tg_approval.request(article)

            if not approved:
                log.info(f"⏭️  Skipped: {article.get('title','')[:60]}")
                db.mark_articles_status([article["id"]], "skipped")
                summary["skipped"] += 1
                continue

            summary["approved"] += 1
            log.info(f"✅ Posting: {article.get('title','')[:60]}")

            # ── Post to all platforms ──────────────────
            text       = self._build_post_text(article)
            link       = article.get("link", "")
            image_path = str(config.DUMMY_IMAGE)

            for platform in self._platforms:
                if platform.name == "telegram":
                    continue  # Telegram used for approval, not posting here
                try:
                    result = platform.send(text=text, image_path=image_path, link=link)
                    db.log_publish(
                        article_id       = article["id"],
                        platform         = platform.name,
                        platform_post_id = result.platform_post_id,
                        status           = "published" if result.success else "failed",
                        error_msg        = result.error_message,
                    )
                    if result.success:
                        summary["posted"] += 1
                        log.info(f"  ✅ {platform.name}: {result.platform_post_id}")
                    else:
                        summary["errors"] += 1
                        log.warning(f"  ❌ {platform.name}: {result.error_message}")
                except Exception as e:
                    summary["errors"] += 1
                    log.error(f"  ❌ {platform.name} exception: {e}")

            db.mark_articles_status([article["id"]], "published")

        log.info(f"📊 Posting done: {summary}")
        return summary

    # ── Portable API: single-article entry point ──────
    def process_incoming(
        self,
        topic: str,
        summary: str,
        full_content: str  = "",
        link: str          = "",
        image_url: str     = "",
        video_url: str     = "",
        priority: str      = "normal",
        source: str        = "webhook",
        tags: list         = None,
    ) -> int:
        """
        Webhook / external API entry point.
        Saves article to DB and immediately runs approval + posting.
        Returns the new article DB id.
        """
        import hashlib
        from datetime import datetime, timezone

        content_hash = hashlib.sha256(f"{topic}{link}".encode()).hexdigest()

        # Save to DB
        with db._conn() as con:
            cur = con.execute(
                f"""INSERT OR IGNORE INTO {config.T_ARTICLES}
                (content_hash, title, link, summary, category, score, status, source_feed, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (content_hash, topic, link, summary,
                 "GENERAL", 5.0, "pending", source,
                 datetime.now(timezone.utc).isoformat()),
            )
            article_id = cur.lastrowid

        if not article_id:
            return 0

        article = {
            "id": article_id, "title": topic, "link": link,
            "summary": summary, "category": "GENERAL",
            "score": 5.0, "image_url": image_url, "video_url": video_url,
        }
        self.post_articles([article])
        return article_id

    # ── Helpers ───────────────────────────────────────

    @staticmethod
    def _build_post_text(article: Dict) -> str:
        """Build the post caption/text from article data."""
        title   = article.get("title", "")
        summary = article.get("summary", "")[:150]
        niche   = config.INSTANCE_DISPLAY
        cat     = article.get("category", "")

        from platforms.telegram import CAT_EMOJI
        emoji = CAT_EMOJI.get(cat, "📰")

        text = f"{emoji} {title}"
        if summary:
            text += f"\n\n{summary}..."
        text += f"\n\n📌 {niche}"
        return text
