"""
blogger.py - Blog Pipeline Orchestrator.

Full flow per article:
  1. Fetch URL content
  2. Generate blog post via AI (with provider fallback)
  3. Generate featured image (Pollinations)
  4. Send to Telegram for approval (staggered, not all at once)
  5. Collect all decisions (long-poll 30s chunks — no tight loop)
  6. For approved: upload image → determine WP category → create WP post
  7. Post FB summary with image + WP link

Telegram approval design:
  - One message sent per blog preview (not flooding)
  - 4s gap between sends (Telegram rate limit safe)
  - Decisions collected in one shared polling loop (30s sleep chunks)
  - No per-article timeout spinning — single shared deadline
  - Unapproved after timeout → skipped (or auto-approved if AUTO_APPROVE=True)

Edge cases:
  - BLOG_ENABLED=False            → entire module is a no-op
  - AI returns None               → log + skip article, others continue
  - WP not configured             → log warning, skip WP post
  - Image gen fails               → use dummy.jpg, continue
  - FB not in platforms           → skip FB post silently
  - Telegram not configured       → auto-approve all, post directly
  - All AI providers blocked      → skip all blogs today, Telegram alert sent
"""
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

import config
import db
from content_fetcher import fetch as fetch_content
from ai_writer import generate as ai_generate
from image_gen import generate as gen_image, build_prompt as img_prompt

log = logging.getLogger("blogger")

_SEND_GAP_SEC   = 4     # gap between sending each approval message
_POLL_CHUNK_SEC = 30    # sleep between each poll sweep
_AUTH_ERRORS    = {401, 403}


# ── Data class ────────────────────────────────────────

@dataclass
class BlogJob:
    article:    dict
    blog_post:  object        = None   # ai_writer.BlogPost
    image_path: Optional[Path] = None
    tg_msg_id:  int           = 0
    decision:   str           = "pending"  # pending|approved|skipped
    wp_post_id: int           = 0
    wp_url:     str           = ""
    error:      str           = ""


# ── Thin Telegram helper (no circular import) ─────────

class _TG:
    """Minimal Telegram calls for blogger (separate from platforms/telegram.py)."""

    def __init__(self):
        self.token   = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        self._offset = 0
        self._ok     = (
            bool(self.token and self.chat_id)
            and not self.token.startswith("1234567890")
        )
        self._dead = False  # set True on 401

    @property
    def usable(self) -> bool:
        return self._ok and not self._dead

    def _post(self, method: str, **kwargs) -> Optional[dict]:
        if not self.usable:
            return None
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.token}/{method}",
                timeout=15, **kwargs
            )
            if r.status_code in _AUTH_ERRORS:
                log.error(f"❌ Telegram auth error — disabling for this run")
                self._dead = True
                return None
            r.raise_for_status()
            data = r.json()
            return data.get("result") if data.get("ok") else None
        except Exception as e:
            log.warning(f"TG {method}: {e}")
            return None

    def send(self, caption: str, image_path: Path,
             keyboard: dict) -> Optional[int]:
        """Send photo+keyboard. Returns message_id or None."""
        import json
        data = {
            "chat_id":     self.chat_id,
            "caption":     caption[:1024],
            "parse_mode":  "HTML",
            "reply_markup": json.dumps(keyboard),
        }
        # Try with image first
        img = image_path or config.DUMMY_IMAGE
        try:
            with open(img, "rb") as f:
                result = self._post("sendPhoto", data=data, files={"photo": f})
            if result:
                return result.get("message_id")
        except Exception:
            pass

        # Fallback: text only
        result = self._post("sendMessage", json={
            "chat_id":     self.chat_id,
            "text":        caption[:4096],
            "parse_mode":  "HTML",
            "reply_markup": __import__("json").dumps(keyboard),
            "disable_web_page_preview": False,
        })
        return result.get("message_id") if result else None

    def plain(self, text: str):
        """Send a plain text message."""
        self._post("sendMessage", json={
            "chat_id": self.chat_id, "text": text,
            "parse_mode": "HTML",
        })

    def remove_keyboard(self, msg_id: int):
        import json
        self._post("editMessageReplyMarkup", json={
            "chat_id":      self.chat_id,
            "message_id":   msg_id,
            "reply_markup": json.dumps({"inline_keyboard": []}),
        })

    def answer_cb(self, cb_id: str, text: str = ""):
        self._post("answerCallbackQuery",
                   json={"callback_query_id": cb_id, "text": text})

    def get_updates(self) -> list:
        result = self._post("getUpdates", json={
            "offset":          self._offset,
            "timeout":         25,  # long-poll at server side
            "allowed_updates": ["callback_query"],
        })
        updates = result or []
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates


# ── Approval captions ─────────────────────────────────

def _caption(job: BlogJob) -> str:
    art  = job.article
    post = job.blog_post
    return (
        f"📝 <b>BLOG PREVIEW</b>  |  {config.INSTANCE_DISPLAY}\n\n"
        f"<b>{post.title}</b>\n\n"
        f"<i>Category:</i> {post.category_hint or 'Auto'}\n"
        f"<i>Tags:</i> {', '.join(post.tags[:5])}\n"
        f"<i>Provider:</i> {post.provider_used}\n\n"
        f"<b>FB Caption Preview:</b>\n{post.fb_summary[:400]}\n\n"
        f"🔗 Source: <a href='{art.get('link','')}'>Original Article</a>"
    )


def _keyboard(article_id: int) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ Publish",    "callback_data": f"blog_approve:{article_id}"},
        {"text": "❌ Skip",       "callback_data": f"blog_skip:{article_id}"},
        {"text": "🚀 Approve All","callback_data": f"blog_all:{article_id}"},
    ]]}


# ── Main orchestrator ─────────────────────────────────

class Blogger:

    def __init__(self):
        self._tg = _TG()

    def run(self, articles: list[dict]) -> dict:
        """
        Run blog pipeline for a list of selected articles.
        Returns summary dict.
        """
        if not config.BLOG_ENABLED:
            log.info("ℹ️  BLOG_ENABLED=False — skipping blog pipeline")
            return {"skipped": len(articles), "reason": "disabled"}

        if not articles:
            return {"skipped": 0}

        summary = {"generated": 0, "approved": 0, "published": 0,
                   "skipped": 0, "errors": 0}

        # ── Phase 1: Prepare all jobs ──────────────────
        jobs: list[BlogJob] = []
        for art in articles:
            job = self._prepare(art)
            if job:
                jobs.append(job)
                summary["generated"] += 1
            else:
                summary["errors"] += 1

        if not jobs:
            log.warning("⚠️  No blog posts generated")
            return summary

        # ── Phase 2: Send all to Telegram (staggered) ──
        jobs = self._send_approvals(jobs)

        # ── Phase 3: Collect all decisions ────────────
        jobs = self._collect_decisions(jobs)

        # ── Phase 4: Publish approved ─────────────────
        for job in jobs:
            if job.decision == "approved":
                ok = self._publish(job)
                if ok:
                    summary["published"] += 1
                    summary["approved"] += 1
                else:
                    summary["errors"] += 1
            else:
                summary["skipped"] += 1
                log.info(f"⏭️  Skipped: {job.blog_post.title[:50]}")

        log.info(f"📊 Blog summary: {summary}")
        return summary

    # ── Phase 1: Fetch + generate ──────────────────────

    def _prepare(self, article: dict) -> Optional[BlogJob]:
        title   = article.get("title", "")
        url     = article.get("link", "")
        summary = article.get("summary", "")
        cat     = article.get("category", "")

        log.info(f"📰 Preparing blog for: {title[:60]}")

        # Fetch URL content
        fetched = fetch_content(url, fallback_summary=summary)
        if not fetched.ok:
            log.warning(f"⚠️  No content for '{title[:50]}' — skipping")
            return None

        # Generate blog with AI
        blog_post = ai_generate(
            article_title  = title,
            source_content = fetched.content,
            source_url     = url,
        )
        if not blog_post:
            log.warning(f"⚠️  AI generation failed for '{title[:50]}'")
            return None

        # Generate image
        prompt = img_prompt(blog_post.title, cat, config.INSTANCE_DISPLAY)
        image  = gen_image(prompt, filename_hint=blog_post.title)

        return BlogJob(
            article    = article,
            blog_post  = blog_post,
            image_path = image,
        )

    # ── Phase 2: Staggered Telegram sends ─────────────

    def _send_approvals(self, jobs: list[BlogJob]) -> list[BlogJob]:
        """
        Send each blog preview to Telegram with _SEND_GAP_SEC gap.
        If Telegram unavailable → mark all approved (auto).
        """
        if not self._tg.usable:
            log.info("ℹ️  Telegram unavailable — auto-approving all blogs")
            for job in jobs:
                job.decision = "approved"
            return jobs

        if config.AUTO_APPROVE:
            log.info("ℹ️  AUTO_APPROVE=True — sending notifications only")
            for job in jobs:
                self._tg.plain(
                    f"🤖 Auto-publishing blog: <b>{job.blog_post.title}</b>"
                )
                job.decision = "approved"
                time.sleep(_SEND_GAP_SEC)
            return jobs

        log.info(f"📤 Sending {len(jobs)} blog previews to Telegram...")
        for i, job in enumerate(jobs):
            msg_id = self._tg.send(
                caption    = _caption(job),
                image_path = job.image_path,
                keyboard   = _keyboard(job.article["id"]),
            )
            if msg_id:
                job.tg_msg_id = msg_id
                log.info(f"  ✉️  Sent preview {i+1}/{len(jobs)}: msg_id={msg_id}")
            else:
                log.warning(f"  ⚠️  Send failed for '{job.blog_post.title[:40]}' — auto-approving")
                job.decision = "approved"

            # Gap between sends — Telegram allows 30 msgs/sec but be polite
            if i < len(jobs) - 1:
                time.sleep(_SEND_GAP_SEC)

        return jobs

    # ── Phase 3: Collect decisions (shared poll loop) ──

    def _collect_decisions(self, jobs: list[BlogJob]) -> list[BlogJob]:
        """
        Single polling loop for all pending jobs.
        Polls in 30s chunks — NOT a tight loop.
        Stops early when all jobs have decisions.
        """
        # Jobs that already have a decision (auto-approved or send failed)
        pending = {j.article["id"]: j for j in jobs if j.decision == "pending"}

        if not pending:
            return jobs

        if not self._tg.usable:
            for job in pending.values():
                job.decision = "approved"
            return jobs

        timeout     = config.BLOG_CONFIG["approval_timeout_sec"]
        deadline    = time.time() + timeout
        approve_all = False
        err_streak  = 0

        log.info(
            f"⏳ Waiting for decisions on {len(pending)} blog(s) "
            f"(timeout={timeout}s, poll every {_POLL_CHUNK_SEC}s)..."
        )

        while pending and time.time() < deadline:
            updates = self._tg.get_updates()

            if updates is None:
                err_streak += 1
                if err_streak >= 3:
                    log.warning("⚠️  3 consecutive Telegram errors — auto-approving remaining")
                    for job in pending.values():
                        job.decision = "approved"
                    return jobs
            else:
                err_streak = 0

            for upd in (updates or []):
                cq   = upd.get("callback_query", {})
                data = cq.get("data", "")
                cb_id = cq.get("id", "")
                parts = data.split(":")
                if len(parts) != 2:
                    continue

                action, art_id_str = parts
                if action not in ("blog_approve", "blog_skip", "blog_all"):
                    continue  # not for us (news approval etc.)

                try:
                    art_id = int(art_id_str)
                except ValueError:
                    continue

                # Could be a job not in our current batch — ignore
                job = pending.get(art_id)
                if not job:
                    continue

                self._tg.answer_cb(cb_id, "✅ Got it!")
                if job.tg_msg_id:
                    self._tg.remove_keyboard(job.tg_msg_id)

                if action == "blog_approve":
                    job.decision = "approved"
                    log.info(f"✅ Blog approved: {job.blog_post.title[:50]}")

                elif action == "blog_skip":
                    job.decision = "skipped"
                    log.info(f"⏭️  Blog skipped: {job.blog_post.title[:50]}")

                elif action == "blog_all":
                    job.decision = "approved"
                    approve_all  = True
                    log.info("🚀 Approve All triggered — approving remaining blogs")

                del pending[art_id]

                # If approve_all: approve everything still pending
                if approve_all:
                    for j in list(pending.values()):
                        j.decision = "approved"
                        if j.tg_msg_id:
                            self._tg.remove_keyboard(j.tg_msg_id)
                    pending.clear()
                    break

            if not pending:
                break

            # Sleep in one chunk — wake when chunk done or deadline near
            remaining = deadline - time.time()
            sleep_for  = min(_POLL_CHUNK_SEC, max(remaining, 0))
            if sleep_for > 0:
                time.sleep(sleep_for)

        # Timeout — apply policy
        if pending:
            policy = "approved" if config.AUTO_APPROVE else "skipped"
            log.info(
                f"⏱️  Timeout: {len(pending)} blog(s) → {policy} "
                f"(AUTO_APPROVE={config.AUTO_APPROVE})"
            )
            for job in pending.values():
                job.decision = policy
                if job.tg_msg_id:
                    self._tg.remove_keyboard(job.tg_msg_id)

        return jobs

    # ── Phase 4: Publish to WP + FB ───────────────────

    def _publish(self, job: BlogJob) -> bool:
        post = job.blog_post
        art  = job.article
        log.info(f"🚀 Publishing to WordPress: '{post.title[:60]}'")

        # Resolve / create WP category
        cat_id = None
        if post.category_hint:
            try:
                import wp_client
                cat_id = wp_client.get_or_create_category(post.category_hint)
            except Exception as e:
                log.warning(f"⚠️  Category lookup failed: {e}")

        # Upload featured image
        media_id = None
        img_path = job.image_path or config.DUMMY_IMAGE
        try:
            import wp_client
            media_id = wp_client.upload_image(img_path)
        except Exception as e:
            log.warning(f"⚠️  Image upload failed: {e}")

        # Create WP post
        wp_result = None
        try:
            import wp_client
            wp_result = wp_client.create_post(
                title             = post.title,
                body_html         = post.body_html,
                tags_list         = post.tags,
                meta_description  = post.meta_description,
                category_id       = cat_id,
                featured_media_id = media_id,
                source_url        = art.get("link", ""),
            )
        except Exception as e:
            log.error(f"❌ WP post creation error: {e}")

        if not wp_result:
            log.error(f"❌ Failed to publish to WordPress: {post.title[:50]}")
            return False

        wp_url = wp_result.get("link", "")
        job.wp_url     = wp_url
        job.wp_post_id = wp_result.get("id", 0)

        log.info(f"✅ Published: {wp_url}")

        # Notify Telegram with published URL
        if self._tg.usable:
            self._tg.plain(
                f"✅ <b>Blog Published</b>\n"
                f"<b>{post.title}</b>\n\n"
                f"🔗 <a href='{wp_url}'>View Post</a>"
            )

        # Post to Facebook
        self._post_fb(job, wp_url)

        return True

    def _post_fb(self, job: BlogJob, wp_url: str):
        """Post FB summary + image + WP link."""
        if "facebook" not in config.ENABLED_PLATFORMS:
            return
        if not wp_url:
            return

        post     = job.blog_post
        img_path = job.image_path or config.DUMMY_IMAGE
        caption  = (
            f"{post.fb_summary}\n\n"
            f"📖 Read full article: {wp_url}"
        )

        try:
            from platforms.facebook import FacebookPlatform
            fb = FacebookPlatform()
            result = fb.send(text=caption, image_path=str(img_path), link=wp_url)
            if result.success:
                log.info(f"✅ FB posted: {result.platform_post_id}")
            else:
                log.warning(f"⚠️  FB post failed: {result.error_message}")
        except Exception as e:
            log.error(f"❌ FB post error: {e}")
