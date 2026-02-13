"""
config.py - Single source of truth for all settings.
Copy this project folder and change INSTANCE_NAME + .env for a new niche.
"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# ── Load .env ─────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent
load_dotenv(PROJECT_ROOT / ".env")

def _get(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()

def _get_bool(key: str, default: bool = False) -> bool:
    return _get(key, str(default)).lower() in ("true", "1", "yes")

def _get_int(key: str, default: int = 0) -> int:
    try:
        return int(_get(key, str(default)))
    except ValueError:
        return default


# ── Identity ──────────────────────────────────────────
# Change this per instance (tech_hindi / english_news / sci_fi / hacker_news)
# All DB tables and vector collections are prefixed with this.
INSTANCE_NAME: str = _get("INSTANCE_NAME", "tech_hindi")
INSTANCE_DISPLAY: str = _get("INSTANCE_DISPLAY", "Tech Hindi")  # For Telegram messages


# ── Paths ─────────────────────────────────────────────
DATA_DIR       = PROJECT_ROOT / "data"
LOGS_DIR       = PROJECT_ROOT / "logs"
MEDIA_DIR      = PROJECT_ROOT / "media"
DB_PATH        = DATA_DIR / f"{INSTANCE_NAME}.db"          # SQLite
CHROMA_DIR     = DATA_DIR / f"{INSTANCE_NAME}_chroma"      # ChromaDB
DUMMY_IMAGE    = MEDIA_DIR / "dummy.jpg"                   # Fallback post image
ROTATION_FILE  = DATA_DIR / f"{INSTANCE_NAME}_rotation.json"  # Backup

for _d in [DATA_DIR, LOGS_DIR, MEDIA_DIR]:
    _d.mkdir(exist_ok=True)


# ── RSS Feeds ─────────────────────────────────────────
RSS_FEEDS = [
    # INDIA: WELFARE & POLITICS
    "https://news.google.com/rss/search?q=india+government+schemes&hl=en-IN&gl=IN&ceid=IN:en",
    "https://www.thehindu.com/news/national/?service=rss",
    "https://feeds.feedburner.com/ndtvnews-top-stories",
    # FINANCE & MARKETS
    "https://www.livemint.com/rss/money",
    "https://economictimes.indiatimes.com/rssfeeds/1286551815.cms",
    # TECHNOLOGY
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml",
    "https://www.sciencedaily.com/rss/top/technology.xml",
    # WORLD & GEOPOLITICS
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://news.google.com/rss/search?q=geopolitics+war+defense&hl=en-US&gl=US&ceid=US:en",
]


# ── Category Anchors (Semantic Targets) ───────────────
CATEGORY_ANCHORS = {
    "WELFARE": {
        "desc": "Indian government schemes, subsidies, ration cards, aadhaar, free grain, farmers welfare, women empowerment, pension schemes, PM Kisan, Awas Yojana.",
        "weight": 14.0,
        "priority": 1,
    },
    "ALERTS": {
        "desc": "Urgent security warning, cyber crime, banking fraud, OTP scams, deepfake, malware, phishing, ransomware, police alert, data breach.",
        "weight": 10.0,
        "priority": 2,
    },
    "WAR_GEO": {
        "desc": "International war, missile attacks, defense military, Russia Ukraine conflict, Israel Gaza Hamas, geopolitics, nuclear threat, NATO operations.",
        "weight": 9.0,
        "priority": 3,
    },
    "POLITICS": {
        "desc": "Parliament session, election results, BJP Congress political news, prime minister speech, new laws passed, court decisions.",
        "weight": 8.0,
        "priority": 4,
    },
    "FINANCE": {
        "desc": "Stock market crash, RBI repo rate, inflation data, GST tax news, gold price, home loan interest, economy GDP, job recruitment.",
        "weight": 7.0,
        "priority": 5,
    },
    "TECH_SCI": {
        "desc": "Artificial intelligence breakthrough, space exploration, ISRO NASA launch, new scientific discovery, future technology, robotics, quantum computing.",
        "weight": 6.0,
        "priority": 6,
    },
    "NOISE": {
        "desc": "Horoscope, zodiac signs, celebrity gossip, dating tips, fashion wardrobe, movie box office collection, cricket match score, viral video.",
        "weight": -100.0,
        "priority": 99,
    },
}


# ── Regex Fallback Patterns ───────────────────────────
REGEX_FALLBACK = {
    "WELFARE": [
        r"\bpm\s?kisan\b", r"\bawas\s?yojana\b", r"\bration\s?card\b",
        r"\bsubsidy\b", r"\bpension\b", r"\baadhaar\b", r"\bpan\s?card\b",
        r"\bfree\s+grain\b", r"\bwomen\s+empowerment\b", r"\bfarmers\b",
    ],
    "ALERTS": [
        r"\bscam\b", r"\bfraud\b", r"\bcyber\s+crime\b", r"\bphishing\b",
        r"\botp\b", r"\bdeepfake\b", r"\bmalware\b", r"\bransomware\b",
        r"\bhack\b", r"\bdata\s+breach\b", r"\balert\b",
    ],
    "WAR_GEO": [
        r"\bukraine\b", r"\brussia\b", r"\bputin\b", r"\bisrael\b",
        r"\bgaza\b", r"\bchina\b", r"\btaiwan\b", r"\bnato\b",
        r"\bmissile\b", r"\bmilitary\b", r"\bnuclear\b", r"\bterror",
    ],
    "TECH_SCI": [
        r"\bartificial\s+intelligence\b", r"\bchatgpt\b", r"\bllm\b",
        r"\bisro\b", r"\bnasa\b", r"\bspacex\b", r"\bquantum\b",
        r"\bsemiconductor\b", r"\binvention\b", r"\bdiscovery\b", r"\bbreakthrough\b",
    ],
    "FINANCE": [
        r"\brbi\b", r"\brepo\s+rate\b", r"\binterest\s+rate\b",
        r"\bgst\b", r"\bsensex\b", r"\bnifty\b", r"\bstock\s+market\b",
        r"\binflation\b", r"\bgdp\b", r"\beconomy\b",
    ],
    "POLITICS": [
        r"\bbjp\b", r"\bcongress\b", r"\bmodi\b", r"\belection\b",
        r"\bparliament\b", r"\bcourt\b", r"\bprotest\b",
    ],
    "NOISE": [
        r"\bhoroscope\b", r"\bzodiac\b", r"\bgossip\b", r"\bwardrobe\b",
        r"\bbox\s+office\b", r"\bcelebrity\b", r"\bcricket\s+score\b",
    ],
}


# ── Local AI Config ───────────────────────────────────
LOCAL_AI_CONFIG = {
    "primary_model":    "all-MiniLM-L6-v2",   # 384-dim, fast, ~80MB
    "fallback_model":   "all-mpnet-base-v2",   # 768-dim, better accuracy
    "batch_size":       32,
    "show_progress":    False,
    "similarity_threshold": 0.85,              # Same-day topic dedup threshold
}


# ── SQLite Table Names (auto-prefixed) ────────────────
T_ARTICLES   = f"{INSTANCE_NAME}_articles"
T_ROTATION   = f"{INSTANCE_NAME}_rotation"
T_PUBLISH    = f"{INSTANCE_NAME}_publish_log"
T_APPROVAL   = f"{INSTANCE_NAME}_approval_queue"

# ChromaDB collection name
CHROMA_COLLECTION = f"{INSTANCE_NAME}_articles"


# ── Pipeline Settings ─────────────────────────────────
PIPELINE = {
    "articles_per_run":    4,       # Articles selected per run
    "top_per_category":    25,      # Candidate pool per category
    "min_score":           0.0,     # Minimum score to consider
    "skip_noise":          True,
    "max_feed_workers":    4,       # Parallel RSS fetch threads
    "max_age_hours":       48,      # Ignore articles older than this
}

# Run schedule (24h format, local time) - 5 times/day
SCHEDULE_TIMES = ["07:00", "10:00", "13:00", "16:30", "19:00"]


# ── Mode Flags ────────────────────────────────────────
TEST_MODE:    bool = _get_bool("TEST_MODE",    False)   # No DB writes, no posts
DRY_RUN:      bool = _get_bool("DRY_RUN",      False)   # DB writes, no posts
AUTO_APPROVE: bool = _get_bool("AUTO_APPROVE", False)   # Skip Telegram approval wait
APPROVAL_TIMEOUT_SEC: int = _get_int("APPROVAL_TIMEOUT_SEC", 300)  # 5 min default


# ── Platform Toggles ──────────────────────────────────
ENABLED_PLATFORMS: list = [
    p.strip() for p in _get("ENABLED_PLATFORMS", "telegram,facebook").split(",") if p.strip()
]
# Possible values: telegram, facebook, instagram, twitter, youtube


# ── Telegram ──────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = _get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID:   str = _get("TELEGRAM_CHAT_ID")


# ── Facebook ──────────────────────────────────────────
FACEBOOK_PAGE_ID:     str = _get("FACEBOOK_PAGE_ID")
FACEBOOK_ACCESS_TOKEN: str = _get("FACEBOOK_ACCESS_TOKEN")


# ── Instagram ─────────────────────────────────────────
INSTAGRAM_ACCOUNT_ID:  str = _get("INSTAGRAM_ACCOUNT_ID")
INSTAGRAM_ACCESS_TOKEN: str = _get("INSTAGRAM_ACCESS_TOKEN")


# ── Twitter / X ───────────────────────────────────────
TWITTER_API_KEY:        str = _get("TWITTER_API_KEY")
TWITTER_API_SECRET:     str = _get("TWITTER_API_SECRET")
TWITTER_ACCESS_TOKEN:   str = _get("TWITTER_ACCESS_TOKEN")
TWITTER_ACCESS_SECRET:  str = _get("TWITTER_ACCESS_SECRET")
TWITTER_BEARER_TOKEN:   str = _get("TWITTER_BEARER_TOKEN")


# ── YouTube ───────────────────────────────────────────
YOUTUBE_CLIENT_ID:     str = _get("YOUTUBE_CLIENT_ID")
YOUTUBE_CLIENT_SECRET: str = _get("YOUTUBE_CLIENT_SECRET")
YOUTUBE_REFRESH_TOKEN: str = _get("YOUTUBE_REFRESH_TOKEN")


# ── Logging ───────────────────────────────────────────
LOG_LEVEL:  str = _get("LOG_LEVEL", "INFO")
LOG_FILE:   Path = LOGS_DIR / f"{INSTANCE_NAME}.log"


# ── Rate Limits ───────────────────────────────────────
RATE_LIMITS = {
    "facebook":  {"requests_per_hour": 200, "retry_attempts": 3, "backoff_base": 2.0},
    "instagram": {"requests_per_hour": 200, "retry_attempts": 3, "backoff_base": 2.0},
    "twitter":   {"requests_per_hour": 300, "retry_attempts": 3, "backoff_base": 2.0},
    "youtube":   {"requests_per_hour": 100, "retry_attempts": 2, "backoff_base": 3.0},
    "telegram":  {"requests_per_hour": 3000, "retry_attempts": 3, "backoff_base": 1.5},
}


# ── Validation ────────────────────────────────────────
def validate() -> list[str]:
    """Return list of config problems. Empty = all good."""
    problems = []

    if not INSTANCE_NAME:
        problems.append("INSTANCE_NAME is required")

    if "telegram" in ENABLED_PLATFORMS:
        if not TELEGRAM_BOT_TOKEN:
            problems.append("TELEGRAM_BOT_TOKEN missing")
        if not TELEGRAM_CHAT_ID:
            problems.append("TELEGRAM_CHAT_ID missing")

    if "facebook" in ENABLED_PLATFORMS:
        if not FACEBOOK_PAGE_ID:
            problems.append("FACEBOOK_PAGE_ID missing")
        if not FACEBOOK_ACCESS_TOKEN:
            problems.append("FACEBOOK_ACCESS_TOKEN missing")

    if "instagram" in ENABLED_PLATFORMS:
        if not INSTAGRAM_ACCOUNT_ID:
            problems.append("INSTAGRAM_ACCOUNT_ID missing")
        if not INSTAGRAM_ACCESS_TOKEN:
            problems.append("INSTAGRAM_ACCESS_TOKEN missing")

    if "twitter" in ENABLED_PLATFORMS:
        missing = [k for k in ["TWITTER_API_KEY", "TWITTER_API_SECRET",
                                "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_SECRET"]
                   if not _get(k)]
        if missing:
            problems.append(f"Twitter missing: {', '.join(missing)}")

    return problems


def print_status():
    """Print current config to console."""
    mode = "🧪 TEST" if TEST_MODE else ("💨 DRY RUN" if DRY_RUN else "🚀 LIVE")
    print(f"\n{'='*55}")
    print(f"  Instance : {INSTANCE_NAME} ({INSTANCE_DISPLAY})")
    print(f"  Mode     : {mode}")
    print(f"  Platforms: {', '.join(ENABLED_PLATFORMS) or 'none'}")
    print(f"  DB       : {DB_PATH}")
    print(f"  Chroma   : {CHROMA_DIR}")
    print(f"  Schedule : {', '.join(SCHEDULE_TIMES)}")
    print(f"{'='*55}\n")
