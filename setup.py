"""
setup.py - First-time setup utility.
Run once after installing requirements:  python setup.py

Creates dirs, dummy image, initialises DB, validates config.
Now also covers blog pipeline dirs + dependency checks.
"""
import sys
import shutil
from pathlib import Path

ROOT = Path(__file__).parent


# ── Directory creation ────────────────────────────────

def create_dirs():
    dirs = [
        ROOT / "data",
        ROOT / "logs",
        ROOT / "media",
        ROOT / "media" / "generated",   # Pollinations-generated images
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
    print("✅ Directories: data/ logs/ media/ media/generated/")


# ── Dummy image ───────────────────────────────────────

def create_dummy_image():
    """Generate a branded placeholder image for posts."""
    try:
        from PIL import Image, ImageDraw
        import config

        img  = Image.new("RGB", (1080, 1080), color=(15, 20, 40))
        draw = ImageDraw.Draw(img)

        for y in range(1080):
            alpha = int(y / 1080 * 60)
            draw.line([(0, y), (1080, y)], fill=(0, 100 + alpha, 200))

        name   = config.INSTANCE_DISPLAY
        lines  = [name, "📰 Latest News", "Stay Informed"]
        y_pos  = 380
        for i, line in enumerate(lines):
            x = max(0, 1080 // 2 - len(line) * 14)
            draw.text((x, y_pos + i * 100), line, fill=(255, 255, 255))

        img.save(config.DUMMY_IMAGE, "JPEG", quality=90)
        print(f"✅ Dummy image: {config.DUMMY_IMAGE}")
        return

    except ImportError:
        pass  # Pillow missing → minimal JPEG below
    except Exception as e:
        print(f"⚠️  Pillow image failed: {e}")

    # Minimal valid 1×1 JPEG fallback
    try:
        import config
        jpeg = bytes([
            0xFF,0xD8,0xFF,0xE0,0x00,0x10,0x4A,0x46,0x49,0x46,0x00,0x01,
            0x01,0x00,0x00,0x01,0x00,0x01,0x00,0x00,0xFF,0xDB,0x00,0x43,
            0x00,0x08,0x06,0x06,0x07,0x06,0x05,0x08,0x07,0x07,0x07,0x09,
            0x09,0x08,0x0A,0x0C,0x14,0x0D,0x0C,0x0B,0x0B,0x0C,0x19,0x12,
            0x13,0x0F,0x14,0x1D,0x1A,0x1F,0x1E,0x1D,0x1A,0x1C,0x1C,0x20,
            0x24,0x2E,0x27,0x20,0x22,0x2C,0x23,0x1C,0x1C,0x28,0x37,0x29,
            0x2C,0x30,0x31,0x34,0x34,0x34,0x1F,0x27,0x39,0x3D,0x38,0x32,
            0x3C,0x2E,0x33,0x34,0x32,0xFF,0xC0,0x00,0x0B,0x08,0x00,0x01,
            0x00,0x01,0x01,0x01,0x11,0x00,0xFF,0xC4,0x00,0x1F,0x00,0x00,
            0x01,0x05,0x01,0x01,0x01,0x01,0x01,0x01,0x00,0x00,0x00,0x00,
            0x00,0x00,0x00,0x00,0x01,0x02,0x03,0x04,0x05,0x06,0x07,0x08,
            0x09,0x0A,0x0B,0xFF,0xDA,0x00,0x08,0x01,0x01,0x00,0x00,0x3F,
            0x00,0xFB,0xD3,0xFF,0xD9,
        ])
        config.DUMMY_IMAGE.write_bytes(jpeg)
        print(f"✅ Minimal dummy image: {config.DUMMY_IMAGE}")
    except Exception as e:
        print(f"⚠️  Could not create dummy image: {e}")


# ── Import checks ─────────────────────────────────────

def check_imports():
    """Check all required and optional packages."""
    checks = [
        # (import_name, display_name, required, purpose)
        ("feedparser",            "feedparser",             True,  "RSS parsing"),
        ("sentence_transformers", "sentence-transformers",  True,  "Local AI embeddings"),
        ("chromadb",              "chromadb",               True,  "Vector DB (same-day dedup)"),
        ("requests",              "requests",               True,  "HTTP client"),
        ("dotenv",                "python-dotenv",          True,  ".env loading"),
        ("PIL",                   "Pillow",                 True,  "Dummy image creation"),
        ("bs4",                   "beautifulsoup4",         True,  "Blog content fetching"),
        ("lxml",                  "lxml",                   False, "Faster HTML parser (optional)"),
        ("requests_oauthlib",     "requests-oauthlib",      False, "Twitter/X (optional)"),
        ("apscheduler",           "apscheduler",            False, "APScheduler (optional)"),
        ("googleapiclient",       "google-api-python-client",False,"YouTube (optional)"),
    ]

    print("\n📦 Package check:")
    missing_required = []
    for imp, name, required, desc in checks:
        try:
            __import__(imp)
            print(f"  ✅ {name:<30} {desc}")
        except ImportError:
            if required:
                print(f"  ❌ {name:<30} {desc} — MISSING (required)")
                missing_required.append(name)
            else:
                print(f"  ⚠️  {name:<30} {desc} — not installed")

    if missing_required:
        print(f"\n  ❗ Install missing packages:")
        print(f"     pip install {' '.join(missing_required)} --break-system-packages\n")
    return missing_required


# ── .env check ────────────────────────────────────────

def check_env():
    env = ROOT / ".env"
    if not env.exists():
        example = ROOT / ".env.example"
        if example.exists():
            shutil.copy(example, env)
            print("✅ .env created from .env.example — EDIT IT before running")
        else:
            print("⚠️  No .env or .env.example found — create .env manually")
    else:
        print("✅ .env exists")


# ── DB init ───────────────────────────────────────────

def init_database():
    try:
        import db
        db.init_db()
        print("✅ Database initialised")
    except Exception as e:
        print(f"❌ DB init failed: {e}")


# ── Config validation ─────────────────────────────────

def validate_config():
    try:
        import config
        problems = config.validate()
        blog_ok  = _check_blog_config(config)

        if problems:
            print(f"\n⚠️  Config issues (edit .env to fix):")
            for p in problems:
                print(f"   • {p}")
        else:
            print("✅ Core config OK")

        if not blog_ok:
            print("ℹ️  Blog pipeline disabled (BLOG_ENABLED=false or missing WP/AI keys)")
        else:
            print("✅ Blog config OK")

    except Exception as e:
        print(f"⚠️  Config check failed: {e}")


def _check_blog_config(config) -> bool:
    """Returns True if blog is enabled and minimally configured."""
    if not getattr(config, "BLOG_ENABLED", False):
        return False

    wp  = getattr(config, "WORDPRESS", {})
    ais = getattr(config, "AI_PROVIDERS", [])

    problems = []
    if not wp.get("url"):
        problems.append("WP_URL missing")
    if not wp.get("username"):
        problems.append("WP_USERNAME missing")
    if not wp.get("app_password"):
        problems.append("WP_APP_PASSWORD missing")

    has_ai = any(p.get("enabled") for p in ais)
    if not has_ai:
        print("  ⚠️  Blog: No AI provider keys set — will use free Pollinations fallback")

    if problems:
        print("  ⚠️  Blog config issues:")
        for p in problems:
            print(f"     • {p}")
        return False

    return True


# ── Main ──────────────────────────────────────────────

def main():
    print("🔧 News AI Pipeline — Setup\n")

    create_dirs()
    check_env()
    missing = check_imports()
    create_dummy_image()
    init_database()
    validate_config()

    print("\n" + "=" * 50)
    if missing:
        print("⚠️  Fix missing packages above then re-run setup.py")
    else:
        print("🚀 Setup complete!")
        print("   Test run : python main.py --test")
        print("   Live run : python main.py --live")
        print("   Schedule : python scheduler.py")
    print("=" * 50 + "\n")


if __name__ == "__main__":
    main()
