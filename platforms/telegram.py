"""
platforms/telegram.py - Telegram Bot for:
  1. Pre-approval of articles (inline keyboard: Approve / Skip / Approve All)
  2. Notification of published posts
  3. Rich message format with niche, category, title, summary, link + image
Uses pure requests (no extra library) for simplicity.
"""
import time
import logging
import requests
from typing import Optional
from pathlib import Path

import config
import db
from platforms.base import BasePlatform, PostResult

log = logging.getLogger("platform.telegram")

# Emoji map per category
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


class TelegramBot:
    """
    Low-level Telegram API wrapper (requests only).
    Does NOT inherit BasePlatform — Telegram is the approval layer,
    not a posting destination in the same way.
    """

    BASE = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self):
        self.token   = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        self._offset  = 0
        self._ok      = bool(self.token and self.chat_id)
        if not self._ok:
            log.warning("⚠️  Telegram not configured (BOT_TOKEN or CHAT_ID missing)")

    def _url(self, method: str) -> str:
        return self.BASE.format(token=self.token, method=method)

    def _post(self, method: str, **kwargs) -> Optional[dict]:
        if not self._ok:
            return None
        try:
            r = requests.post(self._url(method), timeout=15, **kwargs)
            r.raise_for_status()
            data = r.json()
            if data.get("ok"):
                return data["result"]
            log.warning(f"Telegram API error: {data.get('description')}")
        except Exception as e:
            log.error(f"Telegram request failed ({method}): {e}")
        return None

    def send_message(self, text: str, reply_markup: dict = None) -> Optional[dict]:
        payload = {"chat_id": self.chat_id, "text": text,
                   "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_markup:
            import json
            payload["reply_markup"] = json.dumps(reply_markup)
        return self._post("sendMessage", json=payload)

    def send_photo(self, image_path: str, caption: str,
                   reply_markup: dict = None) -> Optional[dict]:
        data = {"chat_id": self.chat_id, "caption": caption,
                "parse_mode": "HTML"}
        if reply_markup:
            import json
            data["reply_markup"] = json.dumps(reply_markup)
        try:
            with open(image_path, "rb") as f:
                return self._post("sendPhoto", data=data,
                                  files={"photo": f})
        except FileNotFoundError:
            log.warning(f"Image not found: {image_path} — sending text only")
            return self.send_message(caption, reply_markup)

    def edit_reply_markup(self, msg_id: int, markup: dict = None):
        import json
        payload = {"chat_id": self.chat_id, "message_id": msg_id}
        if markup:
            payload["reply_markup"] = json.dumps(markup)
        else:
            payload["reply_markup"] = json.dumps({"inline_keyboard": []})
        self._post("editMessageReplyMarkup", json=payload)

    def get_updates(self) -> list:
        """Long-poll for new updates (callback queries)."""
        result = self._post("getUpdates",
                            json={"offset": self._offset, "timeout": 5,
                                  "allowed_updates": ["callback_query"]})
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
    Sends each article to Telegram for human approval.
    Waits up to APPROVAL_TIMEOUT_SEC for a response.
    Falls back to auto-approve on timeout if AUTO_APPROVE=True.
    """

    def __init__(self):
        self._bot = TelegramBot()
        self._approve_all = False  # set True when user taps "Approve All"

    def request(self, article: dict) -> bool:
        """
        Returns True if article is approved, False if skipped.
        """
        if config.AUTO_APPROVE or not self._bot._ok:
            # Just notify, don't wait
            self._notify_only(article)
            return True

        if self._approve_all:
            return True

        caption  = self._build_caption(article)
        keyboard = self._build_keyboard(article["id"])
        image    = str(config.DUMMY_IMAGE)

        msg = self._bot.send_photo(image, caption, reply_markup=keyboard)
        msg_id = msg.get("message_id") if msg else None

        if msg_id:
            db.set_approval(article["id"], tg_msg_id=msg_id)

        # Poll for response
        decision = self._wait_for_decision(
            article["id"], timeout=config.APPROVAL_TIMEOUT_SEC
        )

        if msg_id:
            self._bot.edit_reply_markup(msg_id)  # Remove keyboard after decision

        if decision == "approve_all":
            self._approve_all = True
            return True

        if decision == "approved":
            return True

        if decision == "skipped":
            return False

        # Timeout
        if config.AUTO_APPROVE:
            log.info(f"⏱️  Approval timeout → auto-approved: {article.get('title','')[:50]}")
            return True
        else:
            log.info(f"⏱️  Approval timeout → skipped: {article.get('title','')[:50]}")
            return False

    def _wait_for_decision(self, article_id: int, timeout: int) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            updates = self._bot.get_updates()
            for update in updates:
                cq = update.get("callback_query", {})
                data = cq.get("data", "")
                cq_id = cq.get("id")

                # data format: "approve:123" / "skip:123" / "approve_all:123"
                parts = data.split(":")
                if len(parts) != 2:
                    continue
                action, art_id_str = parts
                if int(art_id_str) != article_id:
                    continue

                # Acknowledge button tap
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

            time.sleep(2)

        return "timeout"

    def _notify_only(self, article: dict):
        """Send notification without waiting (auto-approve mode)."""
        caption = self._build_caption(article)
        caption += "\n\n<i>🤖 Auto-approved</i>"
        self._bot.send_photo(str(config.DUMMY_IMAGE), caption)

    @staticmethod
    def _build_caption(article: dict) -> str:
        cat     = article.get("category", "GENERAL")
        emoji   = CAT_EMOJI.get(cat, "📰")
        niche   = config.INSTANCE_DISPLAY
        title   = article.get("title", "No Title")
        summary = article.get("summary", "")[:200]
        link    = article.get("link", "")
        score   = article.get("score", 0.0)

        return (
            f"<b>📌 {niche}</b> | {emoji} <b>{cat}</b>\n\n"
            f"<b>{title}</b>\n\n"
            f"{summary}{'...' if len(article.get('summary','')) > 200 else ''}\n\n"
            f"🔗 <a href='{link}'>Read full article</a>\n"
            f"<i>Score: {score:.1f}</i>"
        )

    @staticmethod
    def _build_keyboard(article_id: int) -> dict:
        return {
            "inline_keyboard": [[
                {"text": "✅ Approve",     "callback_data": f"approve:{article_id}"},
                {"text": "❌ Skip",        "callback_data": f"skip:{article_id}"},
                {"text": "🚀 Approve All", "callback_data": f"approve_all:{article_id}"},
            ]]
        }


# ── Telegram as a posting platform (notifications) ────

class TelegramPlatform(BasePlatform):
    """
    Post published article notifications to Telegram channel/chat.
    Sends photo + caption with link.
    """

    def __init__(self):
        super().__init__("telegram")
        self._bot = TelegramBot()

    def validate_credentials(self) -> bool:
        return self._bot._ok

    def post_text(self, text: str, link: str = "") -> PostResult:
        msg = self._bot.send_message(f"{text}\n\n🔗 {link}" if link else text)
        if msg:
            return PostResult(success=True, platform="telegram",
                              platform_post_id=str(msg.get("message_id", "")))
        return PostResult(success=False, platform="telegram",
                          error_message="Send failed")

    def post_image(self, text: str, image_path: str, link: str = "") -> PostResult:
        caption = f"{text}\n\n🔗 {link}" if link else text
        msg = self._bot.send_photo(image_path, caption)
        if msg:
            return PostResult(success=True, platform="telegram",
                              platform_post_id=str(msg.get("message_id", "")))
        return PostResult(success=False, platform="telegram",
                          error_message="Send failed")
