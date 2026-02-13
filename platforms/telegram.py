"""
platforms/telegram.py - Telegram Bot:
  1. Pre-approval with inline keyboard (Approve / Skip / Approve All)
  2. Notification posting
  3. Fast-fail on 401 (bad token) — no infinite polling loop
  4. Random delays: 2min base + 10s–90s jitter after each approval
"""
import time
import random
import logging
import requests
from typing import Optional

import config
import db
from platforms.base import BasePlatform, PostResult

log = logging.getLogger("platform.telegram")

CAT_EMOJI = {
    "WELFARE":  "🏛️",
    "ALERTS":   "🚨",
    "WAR_GEO":  "🌍",
    "POLITICS": "🗳️",
    "FINANCE":  "💰",
    "TECH_SCI": "🔬",
    "GENERAL":  "📰",
    "NOISE":    "🗑️",
}

# Errors that mean "wrong credentials" — stop immediately, don't retry
_AUTH_ERRORS = {401, 403}
# Max consecutive getUpdates failures before giving up on approval wait
_MAX_POLL_ERRORS = 3


class TelegramBot:
    """Low-level Telegram Bot API wrapper (requests only)."""

    BASE = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self):
        self.token   = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        self._offset  = 0
        # Check credentials exist
        self._ok = bool(self.token and self.chat_id
                        and self.token != "1234567890:ABCdefGHIjklMNOpqrSTUvwxyz")
        if not self._ok:
            log.warning("⚠️  Telegram not configured — approval/notification disabled")
        # Track if we've already seen a fatal auth error this session
        self._auth_failed = False

    def _url(self, method: str) -> str:
        return self.BASE.format(token=self.token, method=method)

    def _post(self, method: str, **kwargs) -> Optional[dict]:
        """Make a Telegram API call. Returns None on any error."""
        if not self._ok or self._auth_failed:
            return None
        try:
            r = requests.post(self._url(method), timeout=15, **kwargs)

            # Fast-fail on auth errors — don't waste time polling
            if r.status_code in _AUTH_ERRORS:
                log.error(
                    f"❌ Telegram auth error {r.status_code} on '{method}' — "
                    f"check TELEGRAM_BOT_TOKEN. Disabling Telegram for this run."
                )
                self._auth_failed = True
                return None

            r.raise_for_status()
            data = r.json()
            if data.get("ok"):
                return data["result"]
            log.warning(f"Telegram API '{method}' error: {data.get('description')}")
            return None

        except requests.exceptions.Timeout:
            log.warning(f"⏱️  Telegram timeout on '{method}'")
            return None
        except requests.exceptions.ConnectionError as e:
            log.warning(f"🔌 Telegram connection error: {e}")
            return None
        except Exception as e:
            log.error(f"Telegram '{method}' failed: {e}")
            return None

    @property
    def is_usable(self) -> bool:
        return self._ok and not self._auth_failed

    def send_message(self, text: str, reply_markup: dict = None) -> Optional[dict]:
        import json
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        return self._post("sendMessage", json=payload)

    def send_photo(self, image_path: str, caption: str,
                   reply_markup: dict = None) -> Optional[dict]:
        import json
        data = {
            "chat_id": self.chat_id,
            "caption": caption[:1024],  # Telegram caption limit
            "parse_mode": "HTML",
        }
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        try:
            with open(image_path, "rb") as f:
                result = self._post("sendPhoto", data=data, files={"photo": f})
            if result is not None:
                return result
        except FileNotFoundError:
            log.warning(f"Image not found: {image_path} — sending text only")

        # Fallback: send as text if photo fails
        return self.send_message(caption, reply_markup)

    def edit_reply_markup(self, msg_id: int):
        """Remove keyboard from a message after decision."""
        import json
        self._post("editMessageReplyMarkup", json={
            "chat_id": self.chat_id,
            "message_id": msg_id,
            "reply_markup": json.dumps({"inline_keyboard": []}),
        })

    def get_updates(self) -> list:
        """Poll for callback_query updates."""
        result = self._post("getUpdates", json={
            "offset": self._offset,
            "timeout": 4,
            "allowed_updates": ["callback_query"],
        })
        updates = result or []
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates

    def answer_callback(self, callback_query_id: str, text: str = ""):
        self._post("answerCallbackQuery",
                   json={"callback_query_id": callback_query_id, "text": text})


# ── Approval Manager ──────────────────────────────────

class TelegramApproval:
    """
    Sends each article to Telegram for approval before posting.
    Gracefully skips (auto-approves) if Telegram is not configured or fails.

    Delays after each decision:
      - 2 minute base wait (respect Telegram rate limits + look natural)
      - + random 10s–90s jitter per article
    """

    BASE_DELAY_SEC  = 30   # 2 minutes between posts
    JITTER_MIN_SEC  = 10
    JITTER_MAX_SEC  = 60

    def __init__(self):
        self._bot          = TelegramBot()
        self._approve_all  = False

    def request(self, article: dict) -> bool:
        """
        Returns True = post this article, False = skip it.
        Never blocks forever — gives up cleanly on bad config.
        """
        # If Telegram is broken/unconfigured, auto-approve and continue
        if not self._bot.is_usable:
            log.info(f"ℹ️  Telegram unavailable — auto-approving: {article.get('title','')[:50]}")
            return True

        # If user already tapped "Approve All" earlier in this batch
        if self._approve_all:
            self._post_delay()
            return True

        if config.AUTO_APPROVE:
            self._notify_only(article)
            self._post_delay()
            return True

        # Send approval request
        caption  = _build_caption(article)
        keyboard = _build_keyboard(article["id"])
        image    = str(config.DUMMY_IMAGE)

        msg = self._bot.send_photo(image, caption, reply_markup=keyboard)

        # If send itself failed (auth error etc.) — stop trying, auto-approve rest
        if not self._bot.is_usable:
            log.warning("⚠️  Telegram send failed — auto-approving remaining articles")
            return True

        msg_id = msg.get("message_id") if msg else None
        if msg_id:
            db.set_approval(article["id"], tg_msg_id=msg_id)

        # Wait for human decision
        decision = self._wait_for_decision(
            article["id"],
            timeout=config.APPROVAL_TIMEOUT_SEC
        )

        # Remove keyboard buttons after decision
        if msg_id:
            self._bot.edit_reply_markup(msg_id)

        approved = decision in ("approved", "approve_all", "timeout_auto")

        if decision == "approve_all":
            self._approve_all = True

        # Always add delay between articles regardless of decision
        self._post_delay()
        return approved

    def _wait_for_decision(self, article_id: int, timeout: int) -> str:
        """
        Poll Telegram for button taps.
        Gives up after `timeout` seconds or after _MAX_POLL_ERRORS consecutive failures.
        Returns: 'approved' | 'skipped' | 'approve_all' | 'timeout_auto' | 'timeout_skip'
        """
        if not self._bot.is_usable:
            return "timeout_auto"

        deadline      = time.time() + timeout
        error_streak  = 0

        while time.time() < deadline:
            updates = self._bot.get_updates()

            # Stop if auth broke mid-session
            if not self._bot.is_usable:
                log.warning("⚠️  Telegram auth failed during polling — auto-approving")
                return "timeout_auto"

            if updates is None:
                error_streak += 1
                if error_streak >= _MAX_POLL_ERRORS:
                    log.warning(f"⚠️  {_MAX_POLL_ERRORS} consecutive poll errors — auto-approving")
                    return "timeout_auto"
                time.sleep(3)
                continue

            error_streak = 0  # reset on success

            for update in (updates or []):
                cq   = update.get("callback_query", {})
                data = cq.get("data", "")
                cq_id = cq.get("id")
                parts = data.split(":")
                if len(parts) != 2:
                    continue
                action, art_id_str = parts
                try:
                    if int(art_id_str) != article_id:
                        continue
                except ValueError:
                    continue

                self._bot.answer_callback(cq_id, "✅ Got it!")

                if action == "approve":
                    db.update_approval(article_id, "approved")
                    return "approved"
                elif action == "skip":
                    db.update_approval(article_id, "skipped")
                    return "skipped"
                elif action == "approve_all":
                    db.update_approval(article_id, "approve_all")
                    return "approve_all"

            time.sleep(2)  # poll interval

        # Timeout
        if config.AUTO_APPROVE:
            log.info(f"⏱️  Approval timeout ({timeout}s) → auto-approved")
            return "timeout_auto"
        else:
            log.info(f"⏱️  Approval timeout ({timeout}s) → skipped")
            return "timeout_skip"

    def _notify_only(self, article: dict):
        """Send notification without waiting (AUTO_APPROVE mode)."""
        caption = _build_caption(article) + "\n\n<i>🤖 Auto-approved</i>"
        self._bot.send_photo(str(config.DUMMY_IMAGE), caption)

    def _post_delay(self):
        """2min base + random 10–90s jitter. Logs countdown."""
        jitter   = random.randint(self.JITTER_MIN_SEC, self.JITTER_MAX_SEC)
        total    = self.BASE_DELAY_SEC + jitter
        log.info(f"⏳ Post delay: {total}s ({self.BASE_DELAY_SEC}s base + {jitter}s jitter)")

        # Sleep in chunks so logs show progress
        elapsed = 0
        chunk   = 15
        while elapsed < total:
            sleep_for = min(chunk, total - elapsed)
            time.sleep(sleep_for)
            elapsed += sleep_for
            remaining = total - elapsed
            if remaining > 0:
                log.debug(f"   ...{remaining:.0f}s remaining")


# ── Caption / keyboard builders ───────────────────────

def _build_caption(article: dict) -> str:
    cat     = article.get("category", "GENERAL")
    emoji   = CAT_EMOJI.get(cat, "📰")
    niche   = config.INSTANCE_DISPLAY
    title   = article.get("title", "No Title")
    summary = article.get("summary", "")
    link    = article.get("link", "")
    score   = article.get("score", 0.0)
    short_summary = summary[:200] + ("..." if len(summary) > 200 else "")

    return (
        f"<b>📌 {niche}</b>  |  {emoji} <b>{cat}</b>\n\n"
        f"<b>{title}</b>\n\n"
        f"{short_summary}\n\n"
        f"🔗 <a href='{link}'>Read full article</a>\n"
        f"<i>Score: {score:.1f}</i>"
    )


def _build_keyboard(article_id: int) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "✅ Approve",     "callback_data": f"approve:{article_id}"},
            {"text": "❌ Skip",        "callback_data": f"skip:{article_id}"},
            {"text": "🚀 Approve All", "callback_data": f"approve_all:{article_id}"},
        ]]
    }


# ── Telegram as a posting platform ───────────────────

class TelegramPlatform(BasePlatform):
    """Post published article notifications to Telegram."""

    def __init__(self):
        super().__init__("telegram")
        self._bot = TelegramBot()

    def validate_credentials(self) -> bool:
        return self._bot.is_usable

    def post_text(self, text: str, link: str = "") -> PostResult:
        if not self._bot.is_usable:
            return PostResult(False, "telegram", error_message="Telegram not configured")
        msg = self._bot.send_message(f"{text}\n\n🔗 {link}" if link else text)
        if msg:
            return PostResult(True, "telegram",
                              platform_post_id=str(msg.get("message_id", "")))
        return PostResult(False, "telegram", error_message="Send failed")

    def post_image(self, text: str, image_path: str, link: str = "") -> PostResult:
        if not self._bot.is_usable:
            return PostResult(False, "telegram", error_message="Telegram not configured")
        caption = f"{text}\n\n🔗 {link}" if link else text
        msg = self._bot.send_photo(image_path, caption)
        if msg:
            return PostResult(True, "telegram",
                              platform_post_id=str(msg.get("message_id", "")))
        return PostResult(False, "telegram", error_message="Send failed")
