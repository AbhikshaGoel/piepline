"""
wp_client.py - WordPress client.

Supports two modes (auto-detected from WP_URL in config):
  - GraphQL  (WPGraphQL plugin)  → when WP_URL contains /graphql
  - REST API (/wp-json/wp/v2)    → when WP_URL is site root

Your site uses GraphQL: https://littichokhanews.com/graphql

Media uploads always use REST API regardless of mode —
WPGraphQL does not support binary file uploads.

Edge cases:
  - Wrong credentials   → clear error, returns None
  - Category missing    → creates it automatically
  - Image upload fails  → post created without featured image (not blocked)
  - WP not configured   → all methods return None gracefully
"""
import logging
import mimetypes
from pathlib import Path
from typing import Optional

import requests
from requests.auth import HTTPBasicAuth

import config

log = logging.getLogger("wp_client")

_WP      = config.WORDPRESS
_AUTH    = HTTPBasicAuth(_WP["username"], _WP["app_password"]) if _WP.get("username") else None
_TIMEOUT = 30


def _ready() -> bool:
    if not _WP.get("base_url") or not _AUTH:
        log.warning("⚠️  WordPress not configured (WP_URL / WP_USERNAME / WP_APP_PASSWORD)")
        return False
    return True


# ── GraphQL helper ────────────────────────────────────

def _gql(query: str, variables: dict = None) -> Optional[dict]:
    """Execute a GraphQL query/mutation. Returns 'data' dict or None."""
    if not _ready():
        return None
    url = _WP["graphql_url"] or f"{_WP['base_url']}/graphql"
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    try:
        r = requests.post(url, json=payload, auth=_AUTH, timeout=_TIMEOUT)
        if r.status_code in (401, 403):
            log.error("❌ WordPress auth failed — check WP_USERNAME / WP_APP_PASSWORD")
            return None
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            for e in body["errors"]:
                log.warning(f"⚠️  GraphQL error: {e.get('message')}")
            # Return data anyway if partially successful
        return body.get("data")
    except requests.exceptions.Timeout:
        log.warning(f"⏱️  WordPress GraphQL timeout")
    except requests.exceptions.ConnectionError:
        log.warning(f"🔌 WordPress connection error: {url[:60]}")
    except Exception as e:
        log.error(f"❌ WordPress GraphQL error: {e}")
    return None


# ── REST helper (media uploads only) ─────────────────

def _rest(method: str, endpoint: str, **kwargs) -> Optional[dict]:
    """REST API call — used only for media upload."""
    if not _ready():
        return None
    url = f"{_WP['base_url']}/wp-json/wp/v2/{endpoint.lstrip('/')}"
    try:
        r = getattr(requests, method)(url, auth=_AUTH, timeout=_TIMEOUT, **kwargs)
        if r.status_code in (401, 403):
            log.error("❌ WordPress REST auth failed")
            return None
        if r.status_code == 404:
            log.warning(f"⚠️  WordPress REST 404: {endpoint}")
            return None
        r.raise_for_status()
        return r.json()
    except requests.exceptions.Timeout:
        log.warning("⏱️  WordPress REST timeout")
    except Exception as e:
        log.error(f"❌ WordPress REST error: {e}")
    return None


# ── Categories ────────────────────────────────────────

def get_or_create_category(name: str) -> Optional[str]:
    """
    Find WP category by name (case-insensitive) or create it.
    Returns the global GraphQL ID (e.g. 'dGVybTox') or None.
    """
    if not name or not _ready():
        return None

    # Search existing
    data = _gql("""
        query GetCategory($search: String) {
          categories(where: {search: $search}, first: 20) {
            nodes { id databaseId name }
          }
        }
    """, {"search": name})

    if data:
        nodes = data.get("categories", {}).get("nodes", [])
        for node in nodes:
            if node.get("name", "").lower() == name.lower():
                log.info(f"📂 Category found: '{node['name']}' id={node['databaseId']}")
                return node["id"]

    # Create new
    log.info(f"📂 Creating WP category: '{name}'")
    result = _gql("""
        mutation CreateCategory($name: String!) {
          createCategory(input: {name: $name}) {
            category { id databaseId name }
          }
        }
    """, {"name": name})

    if result:
        cat = result.get("createCategory", {}).get("category", {})
        if cat.get("id"):
            log.info(f"✅ Category created: '{cat['name']}' id={cat['databaseId']}")
            return cat["id"]

    log.warning(f"⚠️  Could not create category '{name}' — posting uncategorized")
    return None


# ── Tags ──────────────────────────────────────────────

def _resolve_tags(names: list) -> list:
    """Resolve tag names → GraphQL IDs, creating missing ones."""
    ids = []
    for name in names[:10]:
        name = name.strip()
        if not name:
            continue

        # Search
        data = _gql("""
            query GetTag($search: String) {
              tags(where: {search: $search}, first: 5) {
                nodes { id databaseId name }
              }
            }
        """, {"search": name})

        if data:
            nodes = data.get("tags", {}).get("nodes", [])
            match = next(
                (t for t in nodes if t.get("name","").lower() == name.lower()),
                None,
            )
            if match:
                ids.append(match["id"])
                continue

        # Create
        result = _gql("""
            mutation CreateTag($name: String!) {
              createTag(input: {name: $name}) {
                tag { id databaseId name }
              }
            }
        """, {"name": name})

        if result:
            tag = result.get("createTag", {}).get("tag", {})
            if tag.get("id"):
                ids.append(tag["id"])
    return ids


# ── Media upload (REST — GraphQL doesn't support binary) ──

def upload_image(image_path) -> Optional[str]:
    """
    Upload image via REST API. Returns global GraphQL media ID or None.
    Post continues without featured image if this fails.
    """
    if not image_path:
        return None

    path = Path(image_path)
    if not path.exists():
        log.warning(f"⚠️  Image not found: {path}")
        return None

    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    try:
        with open(path, "rb") as f:
            result = _rest("post", "media",
                headers={
                    "Content-Disposition": f'attachment; filename="{path.name}"',
                    "Content-Type": mime,
                },
                data=f.read(),
            )
        if result and result.get("id"):
            # Convert REST integer ID → GraphQL global ID format
            db_id    = result["id"]
            gql_id   = _int_to_global_id("post", db_id)  # WP media type is "post"
            log.info(f"🖼️  Media uploaded: REST id={db_id}")
            return str(db_id)  # Return as string; createPost accepts databaseId too
    except Exception as e:
        log.warning(f"⚠️  Image upload failed: {e}")
    return None


def _int_to_global_id(type_name: str, db_id: int) -> str:
    """Encode WordPress databaseId to GraphQL global ID."""
    import base64
    return base64.b64encode(f"{type_name}:{db_id}".encode()).decode()


# ── Post creation ─────────────────────────────────────

def create_post(
    title:             str,
    body_html:         str,
    tags_list:         list,
    meta_description:  str  = "",
    category_id:       str  = None,   # GraphQL global ID
    featured_media_id: str  = None,   # REST integer id as string
    status:            str  = None,
    source_url:        str  = "",
) -> Optional[dict]:
    """
    Create a WordPress post via GraphQL.
    Returns dict with 'id', 'databaseId', 'link' on success, else None.
    """
    if not _ready():
        return None

    # Resolve tags
    tag_ids = _resolve_tags(tags_list)

    post_status = (status or _WP.get("default_status", "draft")).upper()
    # GraphQL expects DRAFT | PUBLISH | PENDING | PRIVATE
    if post_status not in ("DRAFT", "PUBLISH", "PENDING", "PRIVATE"):
        post_status = "DRAFT"

    variables = {
        "title":   title,
        "content": body_html,
        "status":  post_status,
        "authorId": str(_WP.get("author_id", 1)),
    }
    if tag_ids:
        variables["tags"]       = {"append": False, "nodes": [{"id": t} for t in tag_ids]}
    if category_id:
        variables["categories"] = {"append": False, "nodes": [{"id": category_id}]}
    if featured_media_id:
        variables["featuredImageId"] = str(featured_media_id)

    result = _gql("""
        mutation CreatePost(
          $title:           String!
          $content:         String
          $status:          PostStatusEnum
          $authorId:        ID
          $tags:            PostTagsInput
          $categories:      PostCategoriesInput
          $featuredImageId: ID
        ) {
          createPost(input: {
            title:           $title
            content:         $content
            status:          $status
            authorId:        $authorId
            tags:            $tags
            categories:      $categories
            featuredImageId: $featuredImageId
          }) {
            post {
              id
              databaseId
              title
              link
              status
            }
          }
        }
    """, variables)

    if result:
        post = result.get("createPost", {}).get("post", {})
        if post.get("databaseId"):
            log.info(
                f"✅ WP post created via GraphQL: "
                f"id={post['databaseId']} status={post['status']} "
                f"url={post.get('link','')}"
            )
            return post

    log.error("❌ WordPress post creation failed")
    return None
