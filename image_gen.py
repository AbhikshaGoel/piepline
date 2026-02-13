"""
image_gen.py - Generate featured images via Pollinations (free, no key).
Same image reused for WP featured image + FB share.

Edge cases:
- Timeout / connection error  → returns None (caller uses dummy.jpg)
- Corrupt / tiny response     → deleted, returns None
- Prompt too long             → truncated to 500 chars
- Already generated today     → returns cached path (no re-download)
"""
import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests

import config

log      = logging.getLogger("image_gen")
_CFG     = config.IMAGE_GEN
_SAVE    = Path(_CFG["save_dir"])
_SAVE.mkdir(parents=True, exist_ok=True)


def generate(prompt: str, filename_hint: str = "") -> Optional[Path]:
    """
    Generate image from prompt. Returns local Path or None.
    Caches by prompt hash — same prompt never re-downloads.
    """
    if not prompt:
        return None

    safe = prompt[:500].replace("\n", " ").strip()
    h    = hashlib.md5(safe.encode()).hexdigest()[:10]
    stem = _slugify(filename_hint)[:40] if filename_hint else ""
    name = f"{stem}_{h}.jpg" if stem else f"{h}.jpg"
    out  = _SAVE / name

    # Return cached
    if out.exists() and out.stat().st_size > 5000:
        log.info(f"🖼️  Cached image: {out.name}")
        return out

    url = (
        f"{_CFG['base_url']}{quote(safe)}"
        f"?width={_CFG['width']}&height={_CFG['height']}"
        f"&model={_CFG['model']}&nologo=true&seed=42"
    )

    log.info(f"🎨 Generating image for: {safe[:60]}...")

    for attempt in range(1, 3):
        try:
            r = requests.get(url, timeout=_CFG["timeout_sec"], stream=True)

            if r.status_code == 200:
                out.write_bytes(r.content)
                if out.stat().st_size < 5000:
                    log.warning("⚠️  Image too small — likely error page")
                    out.unlink(missing_ok=True)
                    return None
                log.info(f"✅ Image saved: {out.name} ({out.stat().st_size // 1024}KB)")
                return out

            log.warning(f"Pollinations HTTP {r.status_code} attempt {attempt}")

        except requests.exceptions.Timeout:
            log.warning(f"⏱️  Image timeout (attempt {attempt})")
        except requests.exceptions.ConnectionError:
            log.warning(f"🔌 Image connection error (attempt {attempt})")
        except Exception as e:
            log.error(f"❌ Image gen error: {e}")
            break

        if attempt < 2:
            time.sleep(4)

    log.warning("⚠️  Image generation failed — fallback to dummy.jpg")
    return None


def build_prompt(title: str, category: str = "", niche: str = "") -> str:
    """Construct a Pollinations-friendly prompt from article metadata."""
    parts = [p for p in [niche, category, title] if p]
    parts.append(
        "professional news featured image, modern design, "
        "vibrant colors, no text overlay, no watermark, photorealistic"
    )
    return ", ".join(parts)


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "_", text)
    return text[:50]
