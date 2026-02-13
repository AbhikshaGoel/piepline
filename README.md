# News AI Pipeline

Modular, local-first news curation + multi-platform posting + AI blog generation.  
Runs 5× per day. No cloud AI for classification. Fully self-contained.

---

## Architecture

### News Pipeline
```
RSS Feeds ──► parser.py ──► ai.py ──► db.py (SQLite + ChromaDB)
                                          │
                               main.py ◄──┘  (rotation + selection)
                                  │
                               poster.py
                              ╱    │     ╲
              Phase 1: Send all   Phase 2: Collect   Phase 3: Post approved
              to Telegram         all decisions       with rate-limit delays
              (staggered 4s)      (30s poll chunks)
```

### Blog Pipeline (optional, runs after news posting)
```
Selected Articles
      │
      ▼
content_fetcher.py  ──► Fetch URL → clean text (BS4)
      │
      ▼
ai_writer.py  ──► Gemini → Groq → Grok → Free (fallback chain)
      │                  └── 429? Block today + Telegram alert + try next
      ▼
image_gen.py  ──► Pollinations API → featured image (1200×630)
      │
      ▼
blogger.py  ──► Telegram approval (staggered send, shared poll loop)
      │
      ├──► wp_client.py  → WordPress REST API (post + category + media)
      │
      └──► Facebook  → FB summary + image + WP link
```

### Stack
| Layer          | Tool                        | Purpose                            |
|----------------|-----------------------------|------------------------------------|
| Feed fetch     | feedparser                  | Parse RSS in parallel              |
| AI classify    | SentenceTransformer (local) | Cosine similarity vs anchors       |
| Fallback       | Regex patterns              | No-model classification            |
| SQL DB         | SQLite                      | Articles, rotation, publish log    |
| Vector DB      | ChromaDB                    | Same-day topic dedup               |
| Approval       | Telegram Bot                | Human-in-the-loop (batch + poll)   |
| Posting        | Graph API / Twitter v2      | Multi-platform dispatch            |
| Blog AI        | Gemini / Groq / Grok / Free | Blog post generation               |
| Blog image     | Pollinations API            | Featured image (free, no key)      |
| Blog publish   | WordPress REST API          | Auto-create post + category + tags |

---

## Quick Start

```bash
# 1. Install
pip install -r requirements.txt --break-system-packages

# 2. Configure
cp .env.example .env
# Edit .env — set INSTANCE_NAME, TELEGRAM_BOT_TOKEN, etc.

# 3. Setup (creates dirs, DB, dummy image, checks deps)
python setup.py

# 4. Test run (no DB writes, no posting)
python main.py --test

# 5. Live run
python main.py --live

# 6. Auto-scheduled (5× per day)
python scheduler.py
```

---

## Blog Pipeline Setup

Enable in `.env`:

```env
BLOG_ENABLED=true

# AI — pick one or more (tried in order, skips rate-limited ones)
GEMINI_API_KEY=AIza...
GROQ_API_KEY=gsk_...
GROK_API_KEY=xai-...
# If none set → free Pollinations text API used as fallback

# WordPress
WP_URL=https://yourblog.com
WP_USERNAME=your_wp_username
WP_APP_PASSWORD=xxxx xxxx xxxx xxxx xxxx xxxx
WP_POST_STATUS=draft        # or: publish
WP_AUTHOR_ID=1

# Optional tuning
BLOG_LANGUAGE=English
BLOG_TONE=informative, friendly, SEO-optimized
```

**WordPress Application Password:**
WP Admin → Users → Profile → Application Passwords → Add New.

**Blog flow per article:**
1. Fetch URL content (falls back to article summary if blocked/timeout)
2. AI generates: title, HTML body, tags, meta description, FB caption
3. Pollinations generates featured image (cached by prompt hash)
4. Telegram approval sent (staggered, not flooding)
5. On approve: upload image → resolve/create WP category → create post
6. Facebook: FB caption + image + WP link posted

**AI rate limit handling:**
- Provider hits 429 → blocked for today (JSON file)
- Telegram alert sent immediately
- Next provider tried automatically
- All blocked → blog skipped for the day, Telegram alert sent
- New calendar day → all blocks auto-cleared

---

## Telegram Approval Flow

### News approval (batch)
1. All selected articles sent to Telegram with 4s gap (not flooding)
2. Single shared polling loop waits for decisions (30s sleep chunks — no tight loop)
3. Tap **✅ Approve**, **❌ Skip**, or **🚀 Approve All** on any article
4. All decisions collected, then posting begins with rate-limit delays
5. Timeout → auto-approve if `AUTO_APPROVE=true`, else skip

### Blog approval (same chat, separate callbacks)
- `blog_approve` / `blog_skip` / `blog_all` callbacks (distinct from news)
- Both news and blog approvals can coexist in same Telegram chat
- 10-minute timeout for blogs (longer than news)

```env
AUTO_APPROVE=true       # Skip waiting, post everything automatically
APPROVAL_TIMEOUT_SEC=300  # How long to wait per batch (seconds)
```

---

## Same-Day Dedup

Before saving any article, two checks:
1. **Hash check** (SQLite) — exact duplicate by content hash
2. **Vector similarity** (ChromaDB) — cosine ≥ 0.92 with any article saved today

Prevents "India election news" and "Election results India" both posting same day.  
Threshold in `config.py` → `LOCAL_AI_CONFIG["similarity_threshold"]`.

---

## Category Rotation

Each run starts from a different category, cycling through all 6:
```
Run 0 → WELFARE  → ALERTS → WAR_GEO → POLITICS → FINANCE → TECH_SCI
Run 1 → ALERTS   → WAR_GEO → POLITICS → FINANCE → TECH_SCI → WELFARE
Run 2 → WAR_GEO  → POLITICS → FINANCE → TECH_SCI → WELFARE → ALERTS
...
```
Rotation advances **before posting** — crash/interrupt never breaks it.  
State stored in SQLite, survives restarts.

---

## Article Status Flow

```
pending → selected → published
                   → skipped
                   → failed
```

Articles are marked `selected` immediately after picking — re-runs never re-select the same articles even if posting was interrupted.

---

## Multi-Tenant (Multiple Pages/Niches)

```bash
cp -r news_pipeline/ hindi_news/
cd hindi_news/
# Edit .env:
#   INSTANCE_NAME=hindi_news
#   INSTANCE_DISPLAY=Hindi News
#   ENABLED_PLATFORMS=telegram,facebook
#   WP_URL=https://hindiblog.com
# Edit config.py: update RSS_FEEDS and CATEGORY_ANCHORS
python setup.py
python scheduler.py
```

All SQLite tables and ChromaDB collections prefixed with `INSTANCE_NAME`.  
Each instance has its own `.env`, its own WP site, its own AI rate limit tracker.

---

## CLI Reference

```
python main.py                      # Dry run — no DB, no posting
python main.py --live               # Full live run
python main.py --test               # Verbose, no writes (safe for dev)
python main.py --status             # Show DB stats + rotation state
python main.py --reset-rotation     # Reset category rotation to 0
python main.py --requeue-failed     # Reset 'selected'/'failed' → 'pending'
python main.py --live --limit 6     # Select 6 articles instead of 4
python main.py --live --keep-noise  # Don't filter NOISE category

python scheduler.py                 # Run on schedule (5× per day)
python setup.py                     # First-time setup / re-check deps
python -m pytest tests/ -v          # Run all tests
```

---

## File Structure

```
news_pipeline/
├── config.py              # All settings — single source of truth
├── main.py                # Pipeline orchestrator + CLI
├── parser.py              # RSS feed fetcher (parallel)
├── ai.py                  # Local AI classify + regex fallback
├── db.py                  # SQLite + ChromaDB data layer
├── poster.py              # Staggered approval + multi-platform dispatch
├── blogger.py             # Blog pipeline orchestrator
├── content_fetcher.py     # URL → clean text (BS4 + regex fallback)
├── ai_writer.py           # Multi-provider AI blog generator
├── image_gen.py           # Pollinations image generation
├── wp_client.py           # WordPress REST API client
├── scheduler.py           # 5× daily job runner
├── setup.py               # First-time setup utility
├── requirements.txt
├── .env.example
├── platforms/
│   ├── base.py            # Retry, rate-limit, dry-run logic
│   ├── telegram.py        # Approval bot + notification platform
│   ├── facebook.py        # Graph API
│   ├── instagram.py       # Graph API (container/publish)
│   ├── twitter.py         # API v2 + OAuth1
│   └── youtube.py         # Data API v3
├── tests/
│   └── test_pipeline.py
├── data/
│   ├── {instance}.db          # SQLite (auto-created)
│   ├── {instance}_chroma/     # ChromaDB (auto-created)
│   └── {instance}_ai_rate_limits.json  # Blog AI block state
├── logs/                  # Log files (auto-created)
└── media/
    ├── dummy.jpg           # Fallback post image (setup.py)
    └── generated/          # Pollinations images (auto-created)
```

---

## Environment Variables Reference

```env
# ── Identity ──────────────────────────────────────────
INSTANCE_NAME=tech_hindi
INSTANCE_DISPLAY=Tech Hindi News

# ── Mode ──────────────────────────────────────────────
TEST_MODE=false
DRY_RUN=false
AUTO_APPROVE=false
APPROVAL_TIMEOUT_SEC=300

# ── Telegram ──────────────────────────────────────────
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# ── Facebook ──────────────────────────────────────────
FACEBOOK_PAGE_ID=...
FACEBOOK_ACCESS_TOKEN=...

# ── Instagram ─────────────────────────────────────────
INSTAGRAM_ACCOUNT_ID=...
INSTAGRAM_ACCESS_TOKEN=...

# ── Twitter/X ─────────────────────────────────────────
TWITTER_API_KEY=...
TWITTER_API_SECRET=...
TWITTER_ACCESS_TOKEN=...
TWITTER_ACCESS_SECRET=...

# ── Platforms to enable ───────────────────────────────
ENABLED_PLATFORMS=telegram,facebook

# ── AI classification ─────────────────────────────────
EMBEDDING_MODEL=all-MiniLM-L6-v2
TRANSFORMERS_OFFLINE=1       # Add after first model download
ANONYMIZED_TELEMETRY=False   # Disable ChromaDB telemetry

# ── Blog ──────────────────────────────────────────────
BLOG_ENABLED=false
GEMINI_API_KEY=
GROQ_API_KEY=
GROK_API_KEY=
WP_URL=
WP_USERNAME=
WP_APP_PASSWORD=
WP_POST_STATUS=draft
WP_AUTHOR_ID=1
BLOG_LANGUAGE=English
BLOG_TONE=informative, friendly, SEO-optimized
POLLINATIONS_MODEL=flux

# ── Delays (optional tuning) ──────────────────────────
POST_DELAY_BASE_SEC=30
POST_DELAY_JITTER_SEC=60
```
