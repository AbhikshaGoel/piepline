"""
wp_client.py - WordPress REST API client.
Handles: post creation, category lookup/create, featured image upload.

Edge cases:
- Wrong credentials     → clear error log, returns None
- Category not found    → creates it via API
- Image upload fails    → post created without featured image (not blocked)
- WP not configured     → all methods return None gracefully
- Network timeout       → logged, returns None
"""
import logging
import mimetypes
from pathlib import Path
from typing import Optional

import requests
from requests.auth import HTTPBasicAuth

import config

log = logging.getLogger("wp_client")

_WP  = config.WORDPRESS
_BASE = _WP["url"].rstrip("/") + "/wp-json/wp/v2" if _WP.get("url") else ""
_AUTH = HTTPBasicAuth(_WP["username"], _WP["app_password"]) if _WP.get("username") else None
_TIMEOUT = 30


def _ready() -> bool:
    if not _BASE or not _AUTH:
        log.warning("⚠️  WordPress not configured (WP_URL / WP_USERNAME / WP_APP_PASSWORD missing)")
        return False
    return True


def _req(method: str, endpoint: str, **kwargs) -> Optional[dict]:
    """Base request with auth + error handling."""
    if not _ready():
        return None
    url = f"{_BASE}/{endpoint.lstrip('/')}"
    try:
        r = getattr(requests, method)(
            url, auth=_AUTH, timeout=_TIMEOUT, **kwargs
        )
        if r.status_code in (401, 403):
            log.error(f"❌ WordPress auth failed — check WP_USERNAME / WP_APP_PASSWORD")
            return None
        if r.status_code == 404:
            log.warning(f"⚠️  WordPress 404: {endpoint}")
            return None
        r.raise_for_status()
        return r.json()
    except requests.exceptions.Timeout:
        log.warning(f"⏱️  WordPress timeout: {endpoint}")
    except requests.exceptions.ConnectionError:
        log.warning(f"🔌 WordPress connection error: {url[:60]}")
    except requests.exceptions.HTTPError as e:
        log.error(f"❌ WordPress HTTP {e.response.status_code}: {endpoint}")
        try:
            log.debug(f"WP error body: {e.response.json()}")
        except Exception:
            pass
    except Exception as e:
        log.error(f"❌ WordPress unexpected error: {e}")
    return None


# ── Categories ────────────────────────────────────────

def get_or_create_category(name: str) -> Optional[int]:
    """
    Find existing WP category by name (case-insensitive).
    If not found, create it. Returns category ID or None.
    """
    if not name or not _ready():
        return None

    # Search existing
    data = _req("get", "categories", params={"search": name, "per_page": 20})
    if data:
        for cat in data:
            if cat.get("name", "").lower() == name.lower():
                log.info(f"📂 Category found: '{cat['name']}' (id={cat['id']})")
                return cat["id"]

    # Create new category
    log.info(f"📂 Creating new WP category: '{name}'")
    result = _req("post", "categories", json={"name": name})
    if result and result.get("id"):
        log.info(f"✅ Category created: '{name}' (id={result['id']})")
        return result["id"]

    log.warning(f"⚠️  Could not create category '{name}' — will post uncategorized")
    return None


# ── Media upload ──────────────────────────────────────

def upload_image(image_path: Path) -> Optional[int]:
    """
    Upload local image as WP media. Returns media ID or None.
    Post continues without featured image if this fails.
    """
    if not image_path or not image_path.exists():
        return None
    if not _ready():
        return None

    mime = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
    try:
        with open(image_path, "rb") as f:
            data = _req(
                "post", "media",
                headers={
                    "Content-Disposition": f'attachment; filename="{image_path.name}"',
                    "Content-Type": mime,
                },
                data=f.read(),
            )
        if data and data.get("id"):
            log.info(f"🖼️  Media uploaded: id={data['id']}")
            return data["id"]
    except Exception as e:
        log.warning(f"⚠️  Image upload failed: {e}")

    return None


# ── Post creation ─────────────────────────────────────

def create_post(
    title:            str,
    body_html:        str,
    tags_list:        list[str],
    meta_description: str  = "",
    category_id:      int  = None,
    featured_media_id: int = None,
    status:           str  = None,
    source_url:       str  = "",
) -> Optional[dict]:
    """
    Create a WordPress post. Returns response dict with 'id' and 'link' or None.

    Tags are created automatically if they don't exist.
    Yoast SEO meta set if plugin is active (ignored otherwise).
    """
    if not _ready():
        return None

    # Resolve tag IDs
    tag_ids = _resolve_tags(tags_list)

    # Build payload
    post_status = status or _WP.get("default_status", "draft")
    payload: dict = {
        "title":   title,
        "content": body_html,
        "status":  post_status,
        "author":  _WP.get("author_id", 1),
        "tags":    tag_ids,
    }

    if category_id:
        payload["categories"] = [category_id]

    if featured_media_id:
        payload["featured_media"] = featured_media_id

    # Yoast SEO (optional — WP ignores unknown fields)
    if meta_description:
        payload["meta"] = {
            "_yoast_wpseo_metadesc": meta_description,
            "_yoast_wpseo_canonical": source_url,
        }

    result = _req("post", "posts", json=payload)

    if result and result.get("id"):
        log.info(
            f"✅ WP post created: id={result['id']} "
            f"status={post_status} url={result.get('link','')}"
        )
        return result

    log.error("❌ WordPress post creation failed")
    return None


# ── Tags helper ───────────────────────────────────────

def _resolve_tags(names: list[str]) -> list[int]:
    """Resolve tag names → IDs, creating missing ones."""
    ids = []
    for name in names[:10]:
        name = name.strip()
        if not name:
            continue
        # Search
        data = _req("get", "tags", params={"search": name, "per_page": 5})
        if data:
            match = next(
                (t for t in data if t.get("name", "").lower() == name.lower()),
                None,
            )
            if match:
                ids.append(match["id"])
                continue
        # Create
        result = _req("post", "tags", json={"name": name})
        if result and result.get("id"):
            ids.append(result["id"])
    return ids
