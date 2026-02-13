"""
content_fetcher.py - Fetch and clean article content from a URL.
Returns plain text for AI blog generation.

Edge cases handled:
- Paywalled / blocked domains  → fallback to summary
- Timeout / connection error   → fallback to summary
- JS-heavy pages (empty body)  → fallback to summary
- Bad encoding                 → auto-detect
- Redirects                    → followed automatically
- Missing BeautifulSoup        → regex strip fallback
"""
import re
import logging
from urllib.parse import urlparse

import requests

import config

log = logging.getLogger("content_fetcher")

HEADERS = {
    "User-Agent":      config.BLOG_CONFIG["user_agent"],
    "Accept":          "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
}

# Known paywalled domains — skip fetch, use summary only
_BLOCKED = {
    "wsj.com", "ft.com", "bloomberg.com", "nytimes.com",
    "economist.com", "businessinsider.com", "theatlantic.com",
}


class FetchResult:
    def __init__(self, url: str = "", title: str = "", content: str = "",
                 source: str = "fetch", error: str = ""):
        self.url     = url
        self.title   = title
        self.content = content
        self.source  = source   # 'fetch' | 'summary_fallback' | 'failed'
        self.error   = error

    @property
    def ok(self) -> bool:
        return bool(self.content and len(self.content) > 50)

    def __repr__(self):
        return f"FetchResult(source={self.source}, chars={len(self.content)}, ok={self.ok})"


def fetch(url: str, fallback_summary: str = "") -> FetchResult:
    """
    Main entry. Fetch article URL → clean text.
    Always returns a FetchResult; never raises.
    """
    if not url:
        return _fallback(url, fallback_summary, "No URL provided")

    domain = _domain(url)

    if domain in _BLOCKED:
        log.info(f"⏭️  Blocked domain ({domain}) — using summary")
        return _fallback(url, fallback_summary, f"Blocked: {domain}")

    try:
        resp = requests.get(
            url,
            headers=HEADERS,
            timeout=config.BLOG_CONFIG["fetch_timeout_sec"],
            allow_redirects=True,
        )

        # Soft 4xx/5xx — don't raise, just fallback
        if resp.status_code >= 400:
            log.warning(f"HTTP {resp.status_code} for {url[:70]}")
            return _fallback(url, fallback_summary, f"HTTP {resp.status_code}")

        # Fix encoding if garbled
        if resp.encoding and resp.encoding.lower() in ("iso-8859-1", "latin-1"):
            resp.encoding = resp.apparent_encoding

        title, content = _parse(resp.text)

        # Too little content = JS-heavy or paywall
        if len(content) < 100:
            log.warning(f"⚠️  Content too short ({len(content)} chars) — using summary")
            return FetchResult(url, title=title,
                               content=fallback_summary or content,
                               source="summary_fallback")

        # Truncate to avoid huge AI prompts
        max_c = config.BLOG_CONFIG["max_content_chars"]
        if len(content) > max_c:
            content = content[:max_c] + "\n\n[Content truncated]"

        log.info(f"✅ Fetched {len(content)} chars from {domain}")
        return FetchResult(url, title=title, content=content, source="fetch")

    except requests.exceptions.Timeout:
        log.warning(f"⏱️  Timeout fetching {url[:70]}")
    except requests.exceptions.ConnectionError:
        log.warning(f"🔌 Connection error: {url[:70]}")
    except Exception as e:
        log.error(f"❌ Fetch error ({url[:70]}): {e}")

    return _fallback(url, fallback_summary, "Fetch failed")


# ── Helpers ───────────────────────────────────────────

def _fallback(url: str, summary: str, reason: str) -> FetchResult:
    if summary:
        log.info(f"📝 Using summary fallback ({reason})")
        return FetchResult(url, content=summary, source="summary_fallback")
    return FetchResult(url, error=reason, source="failed")


def _parse(html: str) -> tuple[str, str]:
    """Extract title + body text. Falls back to regex if no BS4."""
    try:
        from bs4 import BeautifulSoup, Tag

        soup = BeautifulSoup(html, "html.parser")

        # Title
        title = ""
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        if not title:
            h1 = soup.find("h1")
            title = h1.get_text().strip() if h1 else ""

        # Remove noise
        for tag in soup(["script", "style", "nav", "header", "footer",
                         "aside", "form", "iframe", "noscript",
                         "figure", "figcaption"]):
            tag.decompose()
        for el in soup.find_all(class_=re.compile(
                r"ad|banner|sidebar|related|comment|share|social|cookie|popup",
                re.I)):
            el.decompose()

        # Best content element
        body = (
            soup.find("article") or
            soup.find(attrs={"class": re.compile(r"article|post-content|entry|story", re.I)}) or
            soup.find("main") or
            soup.find("body")
        )
        raw = body.get_text(separator=" ") if body else soup.get_text()
        return title, _clean(raw)

    except ImportError:
        # No BeautifulSoup — regex fallback
        content = re.sub(r"<[^>]+>", " ", html)
        return "", _clean(content)

    except Exception as e:
        log.warning(f"Parse error: {e}")
        return "", ""


def _clean(text: str) -> str:
    """Normalize whitespace, drop junk short lines."""
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if len(line) < 25:
            continue
        alpha = sum(c.isalpha() for c in line) / max(len(line), 1)
        if alpha < 0.35:
            continue
        lines.append(line)
    return " ".join(lines)


def _domain(url: str) -> str:
    try:
        d = urlparse(url).netloc.lower()
        return d.removeprefix("www.")
    except Exception:
        return ""
