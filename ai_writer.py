"""
ai_writer.py - AI Blog Post Generator.

Provider chain (tries in order, skips blocked ones):
  Gemini → Groq → Grok → Free (Pollinations text, no key needed)

Rate limit handling:
  - Provider returns 429 → mark blocked TODAY in JSON file
  - Send Telegram alert immediately
  - Try next provider in chain
  - All blocked → return None (skip blog for the day)
  - New calendar day → all blocks auto-cleared

Returns a BlogPost dataclass with everything WordPress needs.
"""
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

import requests

import config

log = logging.getLogger("ai_writer")


# ── Data class ────────────────────────────────────────

@dataclass
class BlogPost:
    title:            str
    body_html:        str
    tags:             list[str]         = field(default_factory=list)
    meta_description: str               = ""
    category_hint:    str               = ""  # AI suggested WP category name
    fb_summary:       str               = ""  # 5-10 line FB caption
    provider_used:    str               = ""
    source_url:       str               = ""


# ── Rate Limit Tracker ────────────────────────────────

class _RateLimitTracker:
    """
    JSON-backed per-day provider block list.
    Auto-resets on new calendar day.
    """

    def __init__(self):
        self._path: Path = config.BLOG_CONFIG["rate_limit_file"]

    def _load(self) -> dict:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text())
                if data.get("date") == str(date.today()):
                    return data
        except Exception:
            pass
        return {"date": str(date.today()), "blocked": []}

    def _save(self, state: dict):
        try:
            self._path.write_text(json.dumps(state, indent=2))
        except Exception as e:
            log.warning(f"Could not save rate limit state: {e}")

    def is_blocked(self, name: str) -> bool:
        return name in self._load().get("blocked", [])

    def block(self, name: str):
        state = self._load()
        if name not in state["blocked"]:
            state["blocked"].append(name)
            self._save(state)
        log.warning(f"🚫 AI provider '{name}' blocked for today")

    def all_blocked(self, providers: list) -> bool:
        blocked = self._load().get("blocked", [])
        active = [p for p in providers if p.get("enabled") and p["name"] not in blocked]
        return len(active) == 0


_rl = _RateLimitTracker()


# ── Telegram alert (thin, no circular import) ─────────

def _tg_alert(text: str):
    """Send a plain Telegram message. Best-effort, never raises."""
    token    = config.TELEGRAM_BOT_TOKEN
    chat_id  = config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return
    # Skip placeholder token
    if token.startswith("1234567890"):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass


# ── Prompt builder ────────────────────────────────────

def _build_prompt(article_title: str, source_content: str,
                  source_url: str) -> str:
    cfg = config.BLOG_CONFIG
    instance = config.INSTANCE_DISPLAY
    lang  = cfg["language"]
    tone  = cfg["tone"]
    min_w = cfg["min_word_count"]
    max_w = cfg["max_word_count"]

    return f"""You are an expert blog writer for "{instance}".

Write a complete blog post based on the article below.

SOURCE TITLE: {article_title}
SOURCE URL:   {source_url}
SOURCE CONTENT:
---
{source_content[:6000]}
---

REQUIREMENTS:
- Language: {lang}
- Tone: {tone}
- Word count: {min_w}–{max_w} words
- SEO-optimized: use keywords naturally, include H2/H3 subheadings
- HTML format: use <h2>, <h3>, <p>, <ul>, <li>, <strong> tags
- Do NOT include <html>, <head>, <body> wrapper tags

Respond ONLY with valid JSON in this exact format (no markdown, no backticks):
{{
  "title": "SEO-optimized blog title here",
  "body_html": "<h2>Introduction</h2><p>...</p>...",
  "meta_description": "155-char SEO description",
  "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"],
  "category_hint": "suggested WordPress category name",
  "fb_summary": "5-10 line Facebook caption with attention-grabbing first line, brief description, and ending with the article URL: {source_url}"
}}"""


# ── Provider implementations ──────────────────────────

def _call_gemini(provider: dict, prompt: str) -> Optional[str]:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{provider['model']}:generateContent?key={provider['api_key']}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 4096},
    }
    r = requests.post(url, json=payload, timeout=60)
    if r.status_code == 429:
        raise _RateLimitError("gemini")
    r.raise_for_status()
    data = r.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _call_groq(provider: dict, prompt: str) -> Optional[str]:
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {provider['api_key']}",
                 "Content-Type": "application/json"},
        json={
            "model":    provider["model"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "max_tokens":  4096,
        },
        timeout=60,
    )
    if r.status_code == 429:
        raise _RateLimitError("groq")
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _call_grok(provider: dict, prompt: str) -> Optional[str]:
    r = requests.post(
        "https://api.x.ai/v1/chat/completions",
        headers={"Authorization": f"Bearer {provider['api_key']}",
                 "Content-Type": "application/json"},
        json={
            "model":    provider["model"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
        },
        timeout=60,
    )
    if r.status_code == 429:
        raise _RateLimitError("grok")
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _call_free(provider: dict, prompt: str) -> Optional[str]:
    """Pollinations free text API — no key, no rate limit tracking."""
    import urllib.parse
    encoded = urllib.parse.quote(prompt[:3000])  # free endpoint has limits
    url = f"https://text.pollinations.ai/{encoded}"
    r   = requests.get(url, timeout=90)
    r.raise_for_status()
    return r.text


# Map provider name → call function
_CALLERS = {
    "gemini": _call_gemini,
    "groq":   _call_groq,
    "grok":   _call_grok,
    "free":   _call_free,
}


class _RateLimitError(Exception):
    def __init__(self, provider: str):
        self.provider = provider
        super().__init__(f"{provider} rate limited")


# ── Response parser ───────────────────────────────────

def _parse_response(raw: str) -> Optional[dict]:
    """
    Extract JSON from AI response.
    Handles: raw JSON, markdown-fenced JSON, partial JSON.
    """
    if not raw:
        return None

    # Strip markdown fences
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip())

    # Try direct parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Find first {...} block
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    log.warning("⚠️  Could not parse AI JSON response")
    log.debug(f"Raw response: {raw[:300]}")
    return None


def _validate(data: dict) -> bool:
    """Check required fields exist and body is substantial."""
    required = ["title", "body_html", "tags", "fb_summary"]
    for key in required:
        if not data.get(key):
            log.warning(f"⚠️  AI response missing field: {key}")
            return False
    if len(data.get("body_html", "")) < 200:
        log.warning("⚠️  body_html too short")
        return False
    return True


# ── Main entry point ──────────────────────────────────

def generate(article_title: str, source_content: str,
             source_url: str = "") -> Optional[BlogPost]:
    """
    Generate a blog post using the best available AI provider.

    Returns BlogPost on success, None if all providers are blocked/failed.
    Sends a Telegram alert if a provider is rate-limited.
    """
    if not source_content:
        log.error("❌ No source content to generate blog from")
        return None

    providers = config.AI_PROVIDERS
    prompt    = _build_prompt(article_title, source_content, source_url)

    for provider in providers:
        name = provider["name"]

        # Skip disabled (no API key)
        if not provider.get("enabled"):
            log.debug(f"⏭️  {name}: disabled (no key)")
            continue

        # Skip rate-limited for today
        if _rl.is_blocked(name):
            log.info(f"⏭️  {name}: blocked today")
            continue

        caller = _CALLERS.get(name)
        if not caller:
            continue

        log.info(f"🤖 Trying AI provider: {name} / {provider['model']}")

        try:
            raw = caller(provider, prompt)

            if not raw:
                log.warning(f"⚠️  {name} returned empty response")
                continue

            data = _parse_response(raw)
            if not data or not _validate(data):
                log.warning(f"⚠️  {name} response invalid — trying next provider")
                continue

            post = BlogPost(
                title            = data["title"].strip(),
                body_html        = data["body_html"].strip(),
                tags             = data.get("tags", [])[:10],
                meta_description = data.get("meta_description", "")[:155],
                category_hint    = data.get("category_hint", ""),
                fb_summary       = data.get("fb_summary", ""),
                provider_used    = name,
                source_url       = source_url,
            )

            log.info(f"✅ Blog generated via {name}: '{post.title[:60]}'")
            return post

        except _RateLimitError as e:
            # Block this provider for today and alert
            _rl.block(e.provider)
            msg = (
                f"⚠️ <b>{config.INSTANCE_DISPLAY}</b>\n"
                f"AI provider <b>{e.provider}</b> hit rate limit.\n"
                f"Blocked for today. Trying next provider..."
            )
            _tg_alert(msg)
            log.warning(f"Rate limit hit: {e.provider}")
            continue

        except requests.exceptions.Timeout:
            log.warning(f"⏱️  {name} timed out")
            continue

        except requests.exceptions.HTTPError as e:
            log.warning(f"❌ {name} HTTP error: {e.response.status_code}")
            continue

        except Exception as e:
            log.error(f"❌ {name} unexpected error: {e}")
            continue

    # All providers exhausted
    blocked = _rl.all_blocked(providers)
    if blocked:
        msg = (
            f"🚫 <b>{config.INSTANCE_DISPLAY}</b>\n"
            f"All AI providers are rate-limited or unavailable.\n"
            f"Blog generation skipped for today."
        )
        _tg_alert(msg)
        log.error("❌ All AI providers blocked — skipping blog today")
    else:
        log.error("❌ All AI providers failed — check API keys / network")

    return None
