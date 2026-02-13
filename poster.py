"""
poster.py - Posting orchestrator.

News approval flow (redesigned):
  Phase 1: Send all articles to Telegram (staggered, 4s gap)
  Phase 2: Collect all decisions in one shared poll loop (30s chunks)
  Phase 3: Post approved articles with rate-limit-safe delays

Blog flow (runs after news posting):
  Calls blogger.Blogger.run() with the same selected articles.
  Controlled by BLOG_ENABLED in config.

Rate limits used from config.RATE_LIMITS per platform.
"""
import logging
import random
import time
from typing import Optional

import requests

import config
import db

log = logging.getLogger("poster")

_SEND_GAP_SEC    = 4     # between each Telegram approval send
_POLL_CHUNK_SEC  = 30    # sleep per poll iteration (not tight loop)
_AUTH_ERRORS     = {401, 403}
_MAX_ERR_STREAK  = 3


# ── Telegram helper (approval only) ──────────────────

class _TG:
    """Minimal Telegram wrapper for approval flow."""

    def __init__(self):
        self.token   = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        self._offset = 0
        self._dead   = False
        self._ok     = (
            bool(self.token and self.chat_id)
            and not (self.token or "").startswith("1234567890")
        )
        if not self._ok:
            log.warning("⚠️  Telegram not configured — auto-approving all articles")

    @property
    def usable(self) -> bool:
        return self._ok and not self._dead

    def _call(self, method: str, **kwargs) -> Optional[dict]:
        if not self.usable:
            return None
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.token}/{method}",
                timeout=15, **kwargs
            )
            if r.status_code in _AUTH_ERRORS:
                log.error(f"❌ Telegram {r.status_code} — disabling for this run")
                self._dead = True
                return None
            r.raise_for_status()
            d = r.json()
            return d.get("result") if d.get("ok") else None
        except requests.exceptions.Timeout:
            log.warning(f"⏱️  Telegram timeout: {method}")
        except Exception as e:
            log.warning(f"TG {method}: {e}")
        return None

    def send_article(self, article: dict, image_path: str) -> Optional[int]:
        import json
        caption  = _build_caption(article)
        keyboard = _build_keyboard(article["id"])
        data = {
            "chat_id":      self.chat_id,
            "caption":      caption[:1024],
            "parse_mode":   "HTML",
            "reply_markup": json.dumps(keyboard),
        }
        # Try with image
        try:
            with open(image_path, "rb") as f:
                r = self._call("sendPhoto", data=data, files={"photo": f})
            if r:
                return r.get("message_id")
        except Exception:
            pass
        # Fallback: text
        r = self._call("sendMessage", json={
            "chat_id":      self.chat_id,
            "text":         caption[:4096],
            "parse_mode":   "HTML",
            "reply_markup": json.dumps(keyboard),
            "disable_web_page_preview": True,
        })
        return r.get("message_id") if r else None

    def remove_keyboard(self, msg_id: int):
        import json
        self._call("editMessageReplyMarkup", json={
            "chat_id":      self.chat_id,
            "message_id":   msg_id,
            "reply_markup": json.dumps({"inline_keyboard": []}),
        })

    def answer_cb(self, cb_id: str, text: str = "✅"):
        self._call("answerCallbackQuery",
                   json={"callback_query_id": cb_id, "text": text})

    def get_updates(self) -> Optional[list]:
        """Returns list or None on error (caller tracks streak)."""
        r = self._call("getUpdates", json={
            "offset":          self._offset,
            "timeout":         25,
            "allowed_updates": ["callback_query"],
        })
        if r is None:
            return None
        self._offset = r[-1]["update_id"] + 1 if r else self._offset
        return r


# ── Caption + keyboard ────────────────────────────────

_CAT_EMOJI = {
    "WELFARE": "🏛️", "ALERTS": "🚨", "WAR_GEO": "🌍",
    "POLITICS": "🗳️", "FINANCE": "💰", "TECH_SCI": "🔬",
    "GENERAL": "📰",
}


def _build_caption(article: dict) -> str:
    cat     = article.get("category", "GENERAL")
    emoji   = _CAT_EMOJI.get(cat, "📰")
    title   = article.get("title", "")
    summary = article.get("summary", "")[:200]
    link    = article.get("link", "")
    score   = article.get("score", 0.0)
    return (
        f"<b>📌 {config.INSTANCE_DISPLAY}</b>  |  {emoji} <b>{cat}</b>\n\n"
        f"<b>{title}</b>\n\n"
        f"{summary}{'...' if len(article.get('summary',''))>200 else ''}\n\n"
        f"🔗 <a href='{link}'>Read article</a>\n"
        f"<i>Score: {score:.1f}</i>"
    )


def _build_keyboard(article_id: int) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ Approve",      "callback_data": f"approve:{article_id}"},
        {"text": "❌ Skip",         "callback_data": f"skip:{article_id}"},
        {"text": "🚀 Approve All",  "callback_data": f"approve_all:{article_id}"},
    ]]}


# ── Main Poster ───────────────────────────────────────

class Poster:

    def __init__(self):
        self._tg        = None
        self._platforms = []
        self._ready     = False

    def start(self) -> bool:
        from platforms import get_enabled_platforms
        self._platforms = get_enabled_platforms()
        self._tg        = _TG()
        self._ready     = True
        names = [p.name for p in self._platforms]
        log.info(f"✅ Poster ready. Platforms: {names or ['(none)']}")
        return True

    def stop(self):
        self._ready = False

    def post_articles(self, articles: list[dict]) -> dict:
        if not self._ready:
            self.start()

        if config.TEST_MODE:
            log.info(f"🧪 TEST MODE: would post {len(articles)} articles")
            return {"approved": 0, "skipped": 0, "posted": 0, "errors": 0, "test": True}

        if not articles:
            return {"approved": 0, "skipped": 0, "posted": 0, "errors": 0}

        # ── Phase 1: Send all to Telegram (staggered) ──
        pending = self._send_all(articles)

        # ── Phase 2: Collect all decisions ────────────
        decisions = self._collect_all(pending, articles)

        # ── Phase 3: Post approved ones ───────────────
        summary = self._post_approved(articles, decisions)

        # ── Phase 4: Blog pipeline (if enabled) ───────
        if config.BLOG_ENABLED:
            try:
                from blogger import Blogger
                blog_summary = Blogger().run(articles)
                summary["blog"] = blog_summary
            except Exception as e:
                log.error(f"❌ Blog pipeline error: {e}")
                summary["blog"] = {"error": str(e)}

        log.info(f"📊 Post summary: {summary}")
        return summary

    # ── Phase 1: staggered sends ──────────────────────

    def _send_all(self, articles: list[dict]) -> dict:
        """
        Send approval messages to Telegram with gaps.
        Returns dict: article_id → msg_id (0 if send failed).
        """
        pending = {}

        if not self._tg or not self._tg.usable:
            # No Telegram — all auto-approved
            for art in articles:
                pending[art["id"]] = 0
            return pending

        if config.AUTO_APPROVE:
            for art in articles:
                pending[art["id"]] = 0
            return pending

        log.info(f"📤 Sending {len(articles)} articles to Telegram for approval...")

        for i, art in enumerate(articles):
            msg_id = self._tg.send_article(art, str(config.DUMMY_IMAGE))

            if not self._tg.usable:
                # Auth broke mid-send — mark remaining as pending with 0
                for remaining in articles[i:]:
                    pending[remaining["id"]] = 0
                break

            pending[art["id"]] = msg_id or 0
            log.info(f"  ✉️  Sent {i+1}/{len(articles)}: '{art.get('title','')[:40]}' msg_id={msg_id}")

            if i < len(articles) - 1:
                time.sleep(_SEND_GAP_SEC)

        return pending

    # ── Phase 2: shared poll loop ─────────────────────

    def _collect_all(self, pending: dict, articles: list[dict]) -> dict:
        """
        Poll Telegram for decisions on all pending articles.
        Returns dict: article_id → 'approved'|'skipped'
        Sleeps in 30s chunks — no tight loop.
        """
        # Determine which IDs still need decisions
        waiting: dict[int, dict] = {}
        decisions: dict[int, str] = {}

        for art in articles:
            art_id = art["id"]
            if not self._tg or not self._tg.usable or config.AUTO_APPROVE:
                decisions[art_id] = "approved"
            elif pending.get(art_id, 0) == 0 and not self._tg.usable:
                decisions[art_id] = "approved"
            else:
                waiting[art_id] = art

        if not waiting:
            return decisions

        timeout    = config.APPROVAL["timeout_sec"]
        deadline   = time.time() + timeout
        err_streak = 0
        approve_all = False

        log.info(
            f"⏳ Waiting for {len(waiting)} approval(s) "
            f"(timeout={timeout}s, {_POLL_CHUNK_SEC}s poll chunks)..."
        )

        while waiting and time.time() < deadline:
            updates = self._tg.get_updates()

            if updates is None:
                err_streak += 1
                if err_streak >= _MAX_ERR_STREAK:
                    log.warning("⚠️  Telegram poll errors — auto-approving remaining")
                    for art_id in list(waiting):
                        decisions[art_id] = "approved"
                        _cleanup_kb(self._tg, pending.get(art_id, 0))
                    return decisions
                time.sleep(5)
                continue

            err_streak = 0

            for upd in updates:
                cq    = upd.get("callback_query", {})
                data  = cq.get("data", "")
                cb_id = cq.get("id", "")
                parts = data.split(":")
                if len(parts) != 2:
                    continue

                action, id_str = parts
                if action not in ("approve", "skip", "approve_all"):
                    continue  # blog callbacks — ignored here

                try:
                    art_id = int(id_str)
                except ValueError:
                    continue

                if art_id not in waiting:
                    continue

                self._tg.answer_cb(cb_id)
                _cleanup_kb(self._tg, pending.get(art_id, 0))

                if action in ("approve", "approve_all"):
                    decisions[art_id] = "approved"
                    if action == "approve_all":
                        approve_all = True
                else:
                    decisions[art_id] = "skipped"

                del waiting[art_id]

                if approve_all:
                    for rem_id in list(waiting):
                        decisions[rem_id] = "approved"
                        _cleanup_kb(self._tg, pending.get(rem_id, 0))
                    waiting.clear()
                    break

            if not waiting:
                break

            sleep_for = min(_POLL_CHUNK_SEC, max(deadline - time.time(), 0))
            if sleep_for > 0:
                time.sleep(sleep_for)

        # Timeout policy
        if waiting:
            policy = "approved" if config.AUTO_APPROVE else "skipped"
            log.info(f"⏱️  Approval timeout → {policy} for {len(waiting)} article(s)")
            for art_id in waiting:
                decisions[art_id] = policy
                _cleanup_kb(self._tg, pending.get(art_id, 0))

        return decisions

    # ── Phase 3: post approved ────────────────────────

    def _post_approved(self, articles: list[dict], decisions: dict) -> dict:
        summary       = {"approved": 0, "skipped": 0, "posted": 0, "errors": 0}
        post_platforms = [p for p in self._platforms if p.name != "telegram"]

        for art in articles:
            art_id   = art["id"]
            decision = decisions.get(art_id, "skipped")

            if decision != "approved":
                log.info(f"⏭️  Skipped: {art.get('title','')[:50]}")
                db.mark_articles_status([art_id], "skipped")
                summary["skipped"] += 1
                continue

            summary["approved"] += 1

            if config.DRY_RUN:
                log.info(f"[DRY RUN] Would post: {art.get('title','')[:50]}")
                db.mark_articles_status([art_id], "published")
                summary["posted"] += 1
                continue

            text      = _build_post_text(art)
            link      = art.get("link", "")
            image     = str(config.DUMMY_IMAGE)
            any_ok    = False

            for platform in post_platforms:
                try:
                    result = platform.send(text=text, image_path=image, link=link)
                    db.log_publish(
                        article_id       = art_id,
                        platform         = platform.name,
                        platform_post_id = result.platform_post_id,
                        status           = "published" if result.success else "failed",
                        error_msg        = result.error_message,
                    )
                    if result.success:
                        any_ok = True
                        summary["posted"] += 1
                        log.info(f"  ✅ {platform.name}: {result.platform_post_id}")
                    else:
                        summary["errors"] += 1
                        log.warning(f"  ❌ {platform.name}: {result.error_message}")

                    # Rate-limit-safe delay between platforms
                    rl  = config.RATE_LIMITS.get(platform.name, {})
                    gap = rl.get("min_gap_sec", 5)
                    j   = random.randint(0, max(gap // 2, 5))
                    log.debug(f"  ⏳ Platform delay: {gap + j}s")
                    time.sleep(gap + j)

                except Exception as e:
                    summary["errors"] += 1
                    log.error(f"  ❌ {platform.name} exception: {e}")

            status = "published" if (any_ok or not post_platforms) else "failed"
            db.mark_articles_status([art_id], status)

        return summary

    # ── Webhook / external API ────────────────────────

    def process_incoming(self, topic: str, summary: str = "",
                         link: str = "", source: str = "webhook") -> int:
        import hashlib
        from datetime import datetime, timezone
        h = hashlib.sha256(f"{topic}{link}".encode()).hexdigest()
        with db._conn() as con:
            cur = con.execute(
                f"INSERT OR IGNORE INTO {config.T_ARTICLES} "
                "(content_hash,title,link,summary,category,score,status,source_feed,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (h, topic, link, summary, "GENERAL", 5.0, "pending",
                 source, datetime.now(timezone.utc).isoformat()),
            )
            art_id = cur.lastrowid
        if art_id:
            self.post_articles([{
                "id": art_id, "title": topic, "link": link,
                "summary": summary, "category": "GENERAL", "score": 5.0,
            }])
        return art_id or 0


# ── Helpers ───────────────────────────────────────────

def _cleanup_kb(tg: _TG, msg_id: int):
    if tg and tg.usable and msg_id:
        tg.remove_keyboard(msg_id)


def _build_post_text(article: dict) -> str:
    cat     = article.get("category", "")
    emoji   = _CAT_EMOJI.get(cat, "📰")
    title   = article.get("title", "")
    summary = article.get("summary", "")[:150]
    text    = f"{emoji} {title}"
    if summary:
        text += f"\n\n{summary}..."
    text += f"\n\n📌 {config.INSTANCE_DISPLAY}"
    return text
