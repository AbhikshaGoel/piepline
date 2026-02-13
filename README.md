# News AI Pipeline

Modular, local-first news curation + multi-platform posting system.  
Runs 5× per day. No cloud AI. No Supabase. Fully self-contained.

---

## Architecture

```
RSS Feeds ──► parser.py ──► ai.py ──► db.py (SQLite + ChromaDB)
                                         │
                              main.py ◄──┘  (rotation + selection)
                                 │
                              poster.py
                                 │
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                   ▼
       Telegram (approval)   Facebook          Instagram/Twitter/YouTube
```

### Stack
| Layer       | Tool                       | Purpose                        |
|-------------|----------------------------|--------------------------------|
| Feed fetch  | feedparser                 | Parse RSS in parallel          |
| AI classify | SentenceTransformer (local)| Cosine similarity vs anchors   |
| Fallback    | Regex patterns             | No-model classification        |
| SQL DB      | SQLite                     | Articles, rotation, publish log|
| Vector DB   | ChromaDB                   | Same-day topic dedup           |
| Approval    | Telegram Bot               | Human-in-the-loop before post  |
| Posting     | Graph API / Twitter v2     | Multi-platform dispatch        |

---

## Quick Start

```bash
# 1. Install
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env — set INSTANCE_NAME, TELEGRAM_BOT_TOKEN, etc.

# 3. Setup (creates DB, dummy image, checks deps)
python setup.py

# 4. Test run (no DB writes, no posting)
python main.py --test

# 5. Live run
python main.py --live

# 6. Auto-scheduled (5× per day)
python scheduler.py
```

---

## Multi-Tenant (Multiple Pages/Niches)

To create a second instance (e.g. English Science News):

```bash
cp -r news_pipeline/ sci_news/
cd sci_news/
# Edit .env:
#   INSTANCE_NAME=sci_news
#   INSTANCE_DISPLAY=Science News
#   ENABLED_PLATFORMS=telegram,instagram
# Edit config.py: update RSS_FEEDS and CATEGORY_ANCHORS
python setup.py
python scheduler.py
```

All SQLite tables and ChromaDB collections are prefixed with `INSTANCE_NAME`.  
No conflicts between instances.

---

## CLI Reference

```
python main.py                    # Dry run — no DB, no posting
python main.py --live             # Full live run
python main.py --test             # Verbose, no writes (safe for dev)
python main.py --status           # Show DB stats
python main.py --reset-rotation   # Reset category rotation
python main.py --live --limit 6   # Select 6 articles instead of 4
python main.py --live --keep-noise  # Don't filter NOISE category

python scheduler.py               # Run on schedule (5× per day)
python setup.py                   # First-time setup
python -m pytest tests/ -v        # Run all tests
```

---

## Telegram Approval Flow

1. Pipeline selects N articles
2. Bot sends each to your Telegram chat:
   - Photo (dummy image) + caption with niche, category, title, summary, link
3. You tap: **✅ Approve**, **❌ Skip**, or **🚀 Approve All**
4. Approved articles are posted to all enabled platforms
5. Timeout (default 5 min) → auto-approve if `AUTO_APPROVE=true`, else skip

Set `AUTO_APPROVE=true` in `.env` for fully automated posting.

---

## Same-Day Dedup Logic

Before saving any article, two checks run:
1. **Hash check** (SQLite) — exact duplicate by title+URL
2. **Vector similarity** (ChromaDB) — cosine similarity ≥ 0.85 with any article saved today

This prevents "India election news" and "Election results India" from both being posted on the same day.

---

## Category Rotation

Each run starts from a different category, cycling through all 6:
```
Run 1 → WELFARE → ALERTS → WAR_GEO → POLITICS → FINANCE → TECH_SCI
Run 2 → ALERTS  → WAR_GEO → POLITICS → FINANCE → TECH_SCI → WELFARE
Run 3 → WAR_GEO → POLITICS → FINANCE → TECH_SCI → WELFARE → ALERTS
...
```
State is stored in SQLite, survives restarts and redeployments.

---

## File Structure

```
news_pipeline/
├── config.py          # All settings — single source of truth
├── main.py            # Pipeline orchestrator + CLI
├── parser.py          # RSS feed fetcher (parallel)
├── ai.py              # Local AI classify + regex fallback
├── db.py              # SQLite + ChromaDB data layer
├── poster.py          # Approval + multi-platform dispatch
├── scheduler.py       # 5× daily job runner
├── setup.py           # First-time setup utility
├── requirements.txt
├── .env.example
├── platforms/
│   ├── base.py        # Retry, rate-limit, dry-run logic
│   ├── telegram.py    # Approval bot + notification
│   ├── facebook.py    # Graph API
│   ├── instagram.py   # Graph API (container/publish)
│   ├── twitter.py     # API v2 + OAuth1
│   └── youtube.py     # Data API v3
├── tests/
│   └── test_pipeline.py
├── data/              # SQLite DB + ChromaDB (auto-created)
├── logs/              # Log files (auto-created)
└── media/
    └── dummy.jpg      # Placeholder image (auto-created by setup.py)
```
