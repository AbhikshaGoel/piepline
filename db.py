"""
db.py - Data layer.
  SQLite  → articles, rotation state, publish log, approval queue
  ChromaDB → embeddings + same-day topic similarity dedup
"""
import json
import sqlite3
import logging
from datetime import datetime, timezone, date
from collections import defaultdict
from contextlib import contextmanager
from typing import List, Dict, Optional

import config

log = logging.getLogger(__name__)


# ── SQLite helpers ────────────────────────────────────

@contextmanager
def _conn():
    """Thread-safe SQLite connection context manager."""
    con = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")  # Better concurrent writes
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    """Create all tables if they don't exist."""
    with _conn() as con:
        # Articles
        con.execute(f"""
        CREATE TABLE IF NOT EXISTS {config.T_ARTICLES} (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            content_hash TEXT    UNIQUE NOT NULL,
            title        TEXT    NOT NULL,
            link         TEXT,
            summary      TEXT,
            category     TEXT    DEFAULT 'GENERAL',
            score        REAL    DEFAULT 0.0,
            status       TEXT    DEFAULT 'pending',
            source_feed  TEXT,
            created_at   TEXT    NOT NULL,
            published_at TEXT
        )""")

        con.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_{config.INSTANCE_NAME}_hash
        ON {config.T_ARTICLES}(content_hash)""")

        con.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_{config.INSTANCE_NAME}_status_cat
        ON {config.T_ARTICLES}(status, category, score DESC)""")

        # Rotation state (1 row)
        con.execute(f"""
        CREATE TABLE IF NOT EXISTS {config.T_ROTATION} (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            last_index  INTEGER DEFAULT 0,
            run_count   INTEGER DEFAULT 0,
            updated_at  TEXT
        )""")

        con.execute(f"""
        INSERT OR IGNORE INTO {config.T_ROTATION}
        VALUES (1, 0, 0, '{_now()}')""")

        # Publish log
        con.execute(f"""
        CREATE TABLE IF NOT EXISTS {config.T_PUBLISH} (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id       INTEGER,
            platform         TEXT,
            platform_post_id TEXT,
            status           TEXT,
            error_msg        TEXT,
            created_at       TEXT
        )""")

        # Approval queue
        con.execute(f"""
        CREATE TABLE IF NOT EXISTS {config.T_APPROVAL} (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id INTEGER UNIQUE,
            tg_msg_id  INTEGER,
            decision   TEXT    DEFAULT 'pending',
            updated_at TEXT
        )""")

    log.info(f"✅ SQLite ready: {config.DB_PATH}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return date.today().isoformat()


# ── ChromaDB helpers ──────────────────────────────────

_chroma_client = None
_chroma_col    = None

def _get_chroma():
    """Lazy-init ChromaDB (creates local persistent DB)."""
    global _chroma_client, _chroma_col
    if _chroma_col is not None:
        return _chroma_col
    try:
        import chromadb
        _chroma_client = chromadb.PersistentClient(path=str(config.CHROMA_DIR))
        _chroma_col = _chroma_client.get_or_create_collection(
            name=config.CHROMA_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )
        log.info(f"✅ ChromaDB ready: {config.CHROMA_DIR}")
        return _chroma_col
    except ImportError:
        log.warning("⚠️  chromadb not installed — vector dedup disabled")
        return None
    except Exception as e:
        log.warning(f"⚠️  ChromaDB init failed: {e}")
        return None


# ── Article operations ────────────────────────────────

def check_hash_exists(hashes: List[str]) -> set:
    """Return set of hashes already in DB."""
    if not hashes:
        return set()
    placeholders = ",".join("?" * len(hashes))
    with _conn() as con:
        rows = con.execute(
            f"SELECT content_hash FROM {config.T_ARTICLES} WHERE content_hash IN ({placeholders})",
            hashes,
        ).fetchall()
    return {r["content_hash"] for r in rows}


def is_similar_today(embedding: List[float], threshold: float = None) -> bool:
    """
    Check ChromaDB: has a similar article (cosine >= threshold) been
    saved TODAY already? Used to prevent same-topic posting on same day.
    """
    if embedding is None:
        return False

    col = _get_chroma()
    if col is None:
        return False

    thr = threshold or config.LOCAL_AI_CONFIG["similarity_threshold"]
    today = _today()

    try:
        results = col.query(
            query_embeddings=[embedding],
            n_results=1,
            where={"date": today},
            include=["distances"],
        )
        if results["distances"] and results["distances"][0]:
            # Chroma cosine distance: 0=identical, 1=orthogonal
            # similarity = 1 - distance
            distance = results["distances"][0][0]
            similarity = 1.0 - distance
            if similarity >= thr:
                log.debug(f"🔁 Similar article today (sim={similarity:.3f})")
                return True
    except Exception as e:
        log.debug(f"Chroma query skipped: {e}")

    return False


def save_embedding(article_id: int, content_hash: str,
                   embedding: List[float], category: str):
    """Store embedding in ChromaDB with date metadata."""
    col = _get_chroma()
    if col is None or embedding is None:
        return
    try:
        col.upsert(
            ids=[content_hash],
            embeddings=[embedding],
            metadatas=[{"date": _today(), "category": category, "db_id": article_id}],
        )
    except Exception as e:
        log.warning(f"⚠️  Chroma upsert failed: {e}")


def save_articles_batch(articles: List[Dict], skip_noise: bool = True) -> int:
    """
    Deduplicate (hash + same-day vector similarity) then save new articles.
    Returns count of newly saved articles.
    """
    if not articles:
        return 0

    # 1. Filter noise
    if skip_noise:
        articles = [a for a in articles if a.get("category") != "NOISE"]

    # 2. Hash dedup against DB
    hashes = [a["content_hash"] for a in articles if a.get("content_hash")]
    existing = check_hash_exists(hashes)
    new_arts = [a for a in articles if a.get("content_hash") not in existing]

    if not new_arts:
        log.info("💤 All articles already exist in DB")
        return 0

    saved = 0
    with _conn() as con:
        for art in new_arts:
            # 3. Vector similarity dedup (same-day topic check)
            emb = art.get("embedding")
            if is_similar_today(emb):
                log.debug(f"⏭️  Skipped (similar today): {art.get('title','')[:60]}")
                continue

            try:
                cur = con.execute(
                    f"""INSERT OR IGNORE INTO {config.T_ARTICLES}
                    (content_hash, title, link, summary, category, score,
                     status, source_feed, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        art.get("content_hash"),
                        art.get("title"),
                        art.get("link"),
                        art.get("summary"),
                        art.get("category", "GENERAL"),
                        art.get("score", 0.0),
                        "pending",
                        art.get("source_feed"),
                        _now(),
                    ),
                )
                if cur.lastrowid:
                    saved += 1
                    art["_db_id"] = cur.lastrowid
            except sqlite3.IntegrityError:
                pass  # hash collision race — safe to ignore

    # 4. Store embeddings in Chroma (after commit)
    for art in new_arts:
        db_id = art.get("_db_id")
        if db_id and art.get("embedding"):
            save_embedding(db_id, art["content_hash"],
                           art["embedding"], art.get("category", "GENERAL"))

    log.info(f"💾 Saved {saved}/{len(new_arts)} new articles to DB")
    return saved


# ── Rotation ──────────────────────────────────────────

def get_rotation() -> Dict:
    with _conn() as con:
        row = con.execute(
            f"SELECT * FROM {config.T_ROTATION} WHERE id=1"
        ).fetchone()
    return dict(row) if row else {"last_index": 0, "run_count": 0}


def advance_rotation():
    with _conn() as con:
        con.execute(
            f"UPDATE {config.T_ROTATION} SET last_index=last_index+1, "
            f"run_count=run_count+1, updated_at=? WHERE id=1",
            (_now(),),
        )


def reset_rotation():
    with _conn() as con:
        con.execute(
            f"UPDATE {config.T_ROTATION} SET last_index=0, run_count=0, "
            f"updated_at=? WHERE id=1",
            (_now(),),
        )
    log.info("🔄 Rotation reset")


# ── Selection ─────────────────────────────────────────

def get_diverse_top_picks(
    limit: int = 4,
    priority_order: Optional[List[str]] = None,
    top_n: int = 25,
    min_score: float = 0.0,
) -> List[Dict]:
    """
    Round-robin category selection from 'pending' articles.
    Uses rotated priority_order so each run starts on a different category.
    """
    if priority_order is None:
        priority_order = sorted(
            [c for c in config.CATEGORY_ANCHORS if c != "NOISE"],
            key=lambda x: config.CATEGORY_ANCHORS[x]["priority"],
        )

    buckets: Dict[str, List[Dict]] = defaultdict(list)

    with _conn() as con:
        for cat in priority_order:
            rows = con.execute(
                f"""SELECT id, content_hash, title, link, summary, category, score
                FROM {config.T_ARTICLES}
                WHERE status='pending' AND category=? AND score>?
                ORDER BY score DESC LIMIT ?""",
                (cat, min_score, top_n),
            ).fetchall()
            buckets[cat] = [dict(r) for r in rows]

    selected, seen_ids = [], set()

    while len(selected) < limit and any(buckets.values()):
        picked = False
        for cat in priority_order:
            if len(selected) >= limit:
                break
            if buckets[cat]:
                art = buckets[cat].pop(0)
                if art["id"] not in seen_ids:
                    selected.append(art)
                    seen_ids.add(art["id"])
                    picked = True
        if not picked:
            break

    # Fallback: fill remaining by score
    if len(selected) < limit:
        remaining = [a for lst in buckets.values() for a in lst
                     if a["id"] not in seen_ids]
        remaining.sort(key=lambda x: x["score"], reverse=True)
        for art in remaining:
            if len(selected) >= limit:
                break
            selected.append(art)
            seen_ids.add(art["id"])

    log.info(f"✅ Selected {len(selected)} diverse articles")
    return selected


# ── Status updates ────────────────────────────────────

def mark_articles_status(article_ids: List[int], status: str):
    """Update status field for given article IDs."""
    if not article_ids:
        return
    placeholders = ",".join("?" * len(article_ids))
    extra = f", published_at='{_now()}'" if status == "published" else ""
    with _conn() as con:
        con.execute(
            f"UPDATE {config.T_ARTICLES} SET status=?{extra} "
            f"WHERE id IN ({placeholders})",
            [status] + article_ids,
        )


def log_publish(article_id: int, platform: str,
                platform_post_id: str = "", status: str = "published",
                error_msg: str = ""):
    with _conn() as con:
        con.execute(
            f"INSERT INTO {config.T_PUBLISH} "
            "(article_id, platform, platform_post_id, status, error_msg, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (article_id, platform, platform_post_id, status, error_msg, _now()),
        )


# ── Approval queue ────────────────────────────────────

def set_approval(article_id: int, tg_msg_id: int = 0):
    with _conn() as con:
        con.execute(
            f"INSERT OR REPLACE INTO {config.T_APPROVAL} "
            "(article_id, tg_msg_id, decision, updated_at) VALUES (?,?,?,?)",
            (article_id, tg_msg_id, "pending", _now()),
        )


def update_approval(article_id: int, decision: str):
    """decision: 'approved' | 'skipped' | 'approve_all'"""
    with _conn() as con:
        con.execute(
            f"UPDATE {config.T_APPROVAL} SET decision=?, updated_at=? "
            f"WHERE article_id=?",
            (decision, _now(), article_id),
        )


def get_approval(article_id: int) -> str:
    with _conn() as con:
        row = con.execute(
            f"SELECT decision FROM {config.T_APPROVAL} WHERE article_id=?",
            (article_id,),
        ).fetchone()
    return row["decision"] if row else "pending"


# ── Stats ─────────────────────────────────────────────

def get_stats() -> Dict:
    with _conn() as con:
        total = con.execute(
            f"SELECT COUNT(*) as n FROM {config.T_ARTICLES}"
        ).fetchone()["n"]

        by_status = {}
        for row in con.execute(
            f"SELECT status, COUNT(*) as n FROM {config.T_ARTICLES} GROUP BY status"
        ).fetchall():
            by_status[row["status"]] = row["n"]

        by_cat = {}
        for row in con.execute(
            f"SELECT category, COUNT(*) as n FROM {config.T_ARTICLES} GROUP BY category"
        ).fetchall():
            by_cat[row["category"]] = row["n"]

        rotation = get_rotation()

    return {
        "instance":   config.INSTANCE_NAME,
        "total":      total,
        "by_status":  by_status,
        "by_category": by_cat,
        "rotation":   rotation,
    }


def get_recent_posts(limit: int = 20) -> List[Dict]:
    with _conn() as con:
        rows = con.execute(
            f"""SELECT a.id, a.title, a.category, a.status,
                       a.created_at, a.published_at,
                       p.platform, p.platform_post_id
                FROM {config.T_ARTICLES} a
                LEFT JOIN {config.T_PUBLISH} p ON p.article_id = a.id
                ORDER BY a.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
