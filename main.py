"""
main.py - News AI Pipeline Orchestrator.

Usage:
  python main.py                  # Dry run (no DB writes, no posting)
  python main.py --live           # Full live run
  python main.py --test           # Verbose output, no DB or posting
  python main.py --status         # Show DB stats
  python main.py --reset-rotation # Reset category rotation
  python main.py --live --limit 6 # Live run, select 6 articles
"""
import os
import sys
import time
import logging
import argparse
from typing import List, Dict
from collections import defaultdict

import config
import db
from parser import RSSParser
from ai import AIEngine
from poster import Poster

# ── Logging setup ─────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("main")


# ── Rotation (DB-backed) ──────────────────────────────

def get_rotated_order() -> List[str]:
    """Return categories sorted by rotated priority."""
    base = sorted(
        [c for c in config.CATEGORY_ANCHORS if c != "NOISE"],
        key=lambda x: config.CATEGORY_ANCHORS[x]["priority"],
    )
    if not base:
        return base

    state = db.get_rotation()
    idx   = state["last_index"] % len(base)
    rotated = base[idx:] + base[:idx]

    log.info(f"🔄 Run #{state['run_count']} | Starting from: {rotated[0]}")
    log.info(f"   Order: {' → '.join(rotated)}")
    return rotated


# ── Pipeline ──────────────────────────────────────────

class NewsPipeline:

    def __init__(self):
        self.parser = RSSParser()
        self.ai     = AIEngine()
        self.poster = Poster()

    def run(self, limit: int = 4, live: bool = False,
            skip_noise: bool = True) -> Dict:
        t0 = time.time()

        _banner(live)

        # ── Step 1: Fetch ──────────────────────────────
        _step(1, "Fetching RSS feeds...")
        raw = self.parser.parse_feeds(config.RSS_FEEDS)
        log.info(f"  ✅ {len(raw)} unique articles fetched")

        if not raw:
            log.warning("⚠️  No articles from feeds")
            return _result([], t0)

        # ── Step 2: AI Classify ────────────────────────
        _step(2, "AI classification & scoring...")
        processed = self.ai.process_articles(raw)
        log.info(f"  ✅ {len(processed)} articles processed")
        _show_top(processed, n=10)

        # ── Step 3: Save to DB ─────────────────────────
        if live:
            _step(3, "Saving to SQLite + ChromaDB...")
            saved = db.save_articles_batch(processed, skip_noise=skip_noise)
            log.info(f"  ✅ {saved} new articles saved")
        else:
            _step(3, "Skipping DB save (not live)")

        # ── Step 4: Select diverse top picks ──────────
        _step(4, "Selecting diverse top picks...")
        order = get_rotated_order()

        if live:
            selected = db.get_diverse_top_picks(
                limit=limit,
                priority_order=order,
                top_n=config.PIPELINE["top_per_category"],
                min_score=config.PIPELINE["min_score"],
            )
        else:
            # Simulate from in-memory processed articles
            selected = _simulate_selection(processed, limit, order)

        log.info(f"  ✅ {len(selected)} articles selected")
        _show_selection(selected)

        # ── Step 5: Post (Telegram approval → platforms)
        if live and selected:
            _step(5, "Approval & posting...")
            self.poster.start()
            summary = self.poster.post_articles(selected)
            log.info(f"  ✅ Post summary: {summary}")
        else:
            _step(5, "Skipping posting (not live)")

        # ── Step 6: Advance rotation ───────────────────
        if live:
            db.advance_rotation()

        duration = round(time.time() - t0, 2)
        _metrics(raw, processed, selected, duration, live)

        return _result(selected, t0)


# ── Helpers ───────────────────────────────────────────

def _banner(live: bool):
    mode = "🚀 LIVE" if live else "💨 DRY RUN"
    if config.TEST_MODE:
        mode = "🧪 TEST"
    print(f"\n{'='*55}")
    print(f"  NEWS AI PIPELINE  |  {config.INSTANCE_DISPLAY}  |  {mode}")
    print(f"{'='*55}\n")


def _step(n: int, msg: str):
    print(f"\n── Step {n}: {msg}")
    log.info(f"STEP {n}: {msg}")


def _show_top(articles: list, n: int = 10):
    sorted_arts = sorted(articles, key=lambda x: x.get("score", 0), reverse=True)
    print(f"\n  📈 Top {n} by score:")
    for i, a in enumerate(sorted_arts[:n], 1):
        print(
            f"  {i:2d}. [{a.get('category','?'):10s}] "
            f"{a.get('score',0):6.2f} "
            f"({a.get('classification_method','?')[:8]}) "
            f"{a.get('title','')[:90]}"
        )


def _show_selection(articles: list):
    if not articles:
        print("  ⚠️  No articles selected")
        return
    print(f"\n  ✅ FINAL SELECTION ({len(articles)}):")
    for i, a in enumerate(articles, 1):
        print(f"  {i}. [{a.get('category','?')}] {a.get('score',0):.1f} — {a.get('title','')[:80]}")
        if a.get("link"):
            print(f"      🔗 {a['link']}")


def _metrics(raw, processed, selected, duration, live):
    print(f"\n{'='*55}")
    print(f"  📊 METRICS")
    print(f"  Fetched   : {len(raw)}")
    print(f"  Processed : {len(processed)}")
    print(f"  Selected  : {len(selected)}")
    print(f"  Duration  : {duration}s")
    print(f"  Mode      : {'LIVE' if live else 'DRY RUN'}")
    print(f"{'='*55}\n")


def _result(articles, t0) -> Dict:
    return {
        "success":  True,
        "articles": articles,
        "duration": round(time.time() - t0, 2),
    }


def _simulate_selection(articles: list, limit: int,
                        priority_order: List[str]) -> list:
    """
    In-memory round-robin selection for dry runs (mirrors DB logic).
    """
    candidates = [
        a for a in articles
        if a.get("category") != "NOISE" and a.get("score", 0) > 0
    ]
    buckets: Dict[str, list] = defaultdict(list)
    for a in candidates:
        buckets[a.get("category", "GENERAL")].append(a)
    for cat in buckets:
        buckets[cat].sort(key=lambda x: x.get("score", 0), reverse=True)
        buckets[cat] = buckets[cat][:config.PIPELINE["top_per_category"]]

    selected, seen = [], set()

    while len(selected) < limit and any(buckets.values()):
        picked = False
        for cat in priority_order:
            if len(selected) >= limit:
                break
            if buckets[cat]:
                a = buckets[cat].pop(0)
                h = a.get("content_hash", "")
                if h not in seen:
                    selected.append(a)
                    seen.add(h)
                    picked = True
        if not picked:
            break

    # Fallback: fill by score
    if len(selected) < limit:
        remaining = sorted(
            [a for lst in buckets.values() for a in lst
             if a.get("content_hash") not in seen],
            key=lambda x: x.get("score", 0),
            reverse=True,
        )
        for a in remaining:
            if len(selected) >= limit:
                break
            selected.append(a)

    return selected


# ── CLI ───────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="News AI Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--live",           action="store_true",
                        help="Live mode: write DB + post to platforms")
    parser.add_argument("--test",           action="store_true",
                        help="Test mode: no DB writes, verbose output")
    parser.add_argument("--status",         action="store_true",
                        help="Show DB statistics and exit")
    parser.add_argument("--reset-rotation", action="store_true",
                        help="Reset category rotation to start")
    parser.add_argument("--limit",          type=int, default=config.PIPELINE["articles_per_run"],
                        help="Number of articles to select")
    parser.add_argument("--keep-noise",     action="store_true",
                        help="Don't filter out NOISE articles")
    args = parser.parse_args()

    # Init DB always (cheap if already exists)
    db.init_db()

    if args.test:
        config.TEST_MODE = True
        print("🧪 TEST MODE: no DB writes, no posting")

    if args.reset_rotation:
        db.reset_rotation()
        print("🔄 Rotation reset")
        if not args.live and not args.status:
            return 0

    if args.status:
        config.print_status()
        stats = db.get_stats()
        print(f"\n  Total articles : {stats['total']}")
        print(f"  By status      : {stats['by_status']}")
        print(f"  By category    : {stats['by_category']}")
        print(f"  Rotation       : {stats['rotation']}")
        return 0

    config.print_status()

    # Validate config (warn but don't block dry runs)
    problems = config.validate()
    if problems:
        for p in problems:
            log.warning(f"⚠️  Config: {p}")
        if args.live and any("missing" in p.lower() for p in problems):
            log.error("Fix config problems before running live")
            return 1

    pipeline = NewsPipeline()
    result   = pipeline.run(
        limit      = args.limit,
        live       = args.live,
        skip_noise = not args.keep_noise,
    )
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
