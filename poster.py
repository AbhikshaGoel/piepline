"""
poster.py - Orchestrates the posting flow:
  1. Telegram approval per article (inline keyboard or auto-approve)
  2. Post to all enabled platforms
  3. Log results to DB

Handles:
  - Telegram unconfigured → auto-approve, skip Telegram posting
  - Platform credential failures → log error, skip that platform, continue
  - No delay between articles needed here (TelegramApproval handles pacing)
"""
import logging
from typing import List, Dict

import config
import db

log = logging.getLogger("poster")


class Poster:

    def __init__(self):
        self._platforms   = []
        self._tg_approval = None
        self._ready       = False

    def start(self) -> bool:
        """Initialize all enabled platforms and Telegram approval."""
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
        Main entry point. Returns summary dict.

        For each article:
          1. Ask Telegram for approval (or auto-approve if unconfigured)
          2. If approved → post to all non-Telegram platforms
          3. Log results
        """
        if not self._ready:
            self.start()

        # If TEST_MODE or no articles: log and return
        if config.TEST_MODE:
            log.info(f"🧪 TEST MODE: would post {len(articles)} articles (skipped)")
            return {"approved": 0, "skipped": 0, "posted": 0, "errors": 0, "test": True}

        if not articles:
            log.info("💤 No articles to post")
            return {"approved": 0, "skipped": 0, "posted": 0, "errors": 0}

        # Check if any real (non-Telegram) posting platform is available
        post_platforms = [p for p in self._platforms if p.name != "telegram"]
        if not post_platforms and not config.DRY_RUN:
            log.warning("⚠️  No posting platforms configured — articles will be approved only")

        summary = {"approved": 0, "skipped": 0, "posted": 0, "errors": 0}

        for article in articles:
            title = article.get("title", "")[:60]

            # ── Step A: Telegram Approval ──────────────
            approved = True
            if self._tg_approval:
                approved = self._tg_approval.request(article)

            if not approved:
                log.info(f"⏭️  Skipped (user decision): {title}")
                db.mark_articles_status([article["id"]], "skipped")
                summary["skipped"] += 1
                continue

            summary["approved"] += 1
            log.info(f"✅ Approved: {title}")

            # ── Step B: Post to platforms ──────────────
            if config.DRY_RUN:
                log.info(f"[DRY RUN] Would post: {title}")
                db.mark_articles_status([article["id"]], "published")
                summary["posted"] += 1
                continue

            text       = _build_post_text(article)
            link       = article.get("link", "")
            image_path = str(config.DUMMY_IMAGE)

            any_posted = False
            for platform in post_platforms:
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
                        any_posted = True
                        summary["posted"] += 1
                        log.info(f"  ✅ {platform.name}: {result.platform_post_id}")
                    else:
                        summary["errors"] += 1
                        log.warning(f"  ❌ {platform.name}: {result.error_message}")

                except Exception as e:
                    summary["errors"] += 1
                    log.error(f"  ❌ {platform.name} exception: {e}")

            # Mark published if at least one platform succeeded
            # (or if no platforms configured — still mark so it's not re-selected)
            final_status = "published" if (any_posted or not post_platforms) else "failed"
            db.mark_articles_status([article["id"]], final_status)

        log.info(f"📊 Posting done: {summary}")
        return summary


# ── Portable external API ─────────────────────────────

    def process_incoming(
        self,
        topic:        str,
        summary:      str,
        full_content: str  = "",
        link:         str  = "",
        image_url:    str  = "",
        video_url:    str  = "",
        priority:     str  = "normal",
        source:       str  = "webhook",
        tags:         list = None,
    ) -> int:
        """
        External / webhook entry point.
        Saves article and immediately runs approval + posting.
        Returns DB article id.
        """
        import hashlib
        from datetime import datetime, timezone

        content_hash = hashlib.sha256(f"{topic}{link}".encode()).hexdigest()

        with db._conn() as con:
            cur = con.execute(
                f"""INSERT OR IGNORE INTO {config.T_ARTICLES}
                (content_hash, title, link, summary, category, score,
                 status, source_feed, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (content_hash, topic, link, summary,
                 "GENERAL", 5.0, "pending", source,
                 datetime.now(timezone.utc).isoformat()),
            )
            article_id = cur.lastrowid

        if not article_id:
            return 0

        article = {
            "id":        article_id,
            "title":     topic,
            "link":      link,
            "summary":   summary,
            "category":  "GENERAL",
            "score":     5.0,
            "image_url": image_url,
            "video_url": video_url,
        }
        self.post_articles([article])
        return article_id


# ── Post text builder ─────────────────────────────────

def _build_post_text(article: Dict) -> str:
    """Build platform post caption from article data."""
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
