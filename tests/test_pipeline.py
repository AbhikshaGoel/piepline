"""
tests/test_pipeline.py - Core pipeline tests.
Run with: python -m pytest tests/ -v
or:        python main.py --test
"""
import sys
import os
import pytest
import tempfile
import shutil

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ── Fixtures ──────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolate_db(tmp_path, monkeypatch):
    """Use a temp directory for DB so tests don't pollute real data."""
    monkeypatch.setattr("config.DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr("config.CHROMA_DIR", tmp_path / "chroma")
    monkeypatch.setattr("config.TEST_MODE", True)
    import db
    db.init_db()
    yield


SAMPLE_ARTICLE = {
    "title":        "RBI raises repo rate by 25 basis points to control inflation",
    "link":         "https://example.com/rbi-rate",
    "summary":      "Reserve Bank of India raises rates for the third consecutive time.",
    "content_hash": "abc123deadbeef",
    "source_feed":  "https://example.com/rss",
    "published_at": None,
}

SAMPLE_ARTICLES = [
    {**SAMPLE_ARTICLE, "content_hash": f"hash_{i}",
     "title": f"Article {i} about finance and RBI",
     "link": f"https://example.com/{i}"}
    for i in range(10)
]


# ── Parser tests ──────────────────────────────────────

class TestParser:
    def test_clean_html(self):
        from parser import RSSParser
        p = RSSParser()
        assert p._clean_html("<b>Hello</b> <i>World</i>") == "Hello World"

    def test_hash_deterministic(self):
        from parser import RSSParser
        h1 = RSSParser._hash("title", "http://link.com")
        h2 = RSSParser._hash("title", "http://link.com")
        assert h1 == h2

    def test_hash_different_inputs(self):
        from parser import RSSParser
        h1 = RSSParser._hash("title a", "http://a.com")
        h2 = RSSParser._hash("title b", "http://b.com")
        assert h1 != h2


# ── AI Engine tests ───────────────────────────────────

class TestRegexClassifier:
    def test_finance_detection(self):
        from ai import RegexClassifier
        clf = RegexClassifier()
        cat, conf = clf.classify("RBI raises repo rate inflation GDP growth")
        assert cat == "FINANCE"
        assert conf > 0

    def test_alerts_detection(self):
        from ai import RegexClassifier
        clf = RegexClassifier()
        cat, conf = clf.classify("cyber scam fraud OTP phishing attack alert")
        assert cat == "ALERTS"

    def test_noise_detection(self):
        from ai import RegexClassifier
        clf = RegexClassifier()
        cat, conf = clf.classify("horoscope zodiac celebrity gossip wardrobe")
        assert cat == "NOISE"

    def test_unknown_falls_to_general(self):
        from ai import RegexClassifier
        clf = RegexClassifier()
        cat, conf = clf.classify("banana smoothie recipe")
        assert cat == "GENERAL"
        assert conf < 0.5


# ── DB tests ──────────────────────────────────────────

class TestDatabase:
    def test_save_and_retrieve(self):
        import db
        articles = [{
            **SAMPLE_ARTICLES[0],
            "category": "FINANCE",
            "score": 15.0,
            "embedding": None,
        }]
        saved = db.save_articles_batch(articles)
        assert saved == 1

    def test_no_duplicates(self):
        import db
        art = [{**SAMPLE_ARTICLES[0], "category": "FINANCE",
                "score": 10.0, "embedding": None}]
        db.save_articles_batch(art)
        saved_again = db.save_articles_batch(art)
        assert saved_again == 0

    def test_noise_filtered(self):
        import db
        noisy = [{**SAMPLE_ARTICLES[1], "category": "NOISE",
                  "score": -50.0, "embedding": None}]
        saved = db.save_articles_batch(noisy, skip_noise=True)
        assert saved == 0

    def test_rotation_advance(self):
        import db
        state_before = db.get_rotation()
        db.advance_rotation()
        state_after = db.get_rotation()
        assert state_after["run_count"] == state_before["run_count"] + 1
        assert state_after["last_index"] == state_before["last_index"] + 1

    def test_rotation_reset(self):
        import db
        db.advance_rotation()
        db.advance_rotation()
        db.reset_rotation()
        state = db.get_rotation()
        assert state["last_index"] == 0
        assert state["run_count"] == 0

    def test_stats(self):
        import db
        stats = db.get_stats()
        assert "total" in stats
        assert "by_status" in stats
        assert "rotation" in stats


# ── Rotation tests ────────────────────────────────────

class TestRotation:
    def test_rotation_order(self):
        """Verify rotation shifts start category correctly."""
        import db
        import config

        base = sorted(
            [c for c in config.CATEGORY_ANCHORS if c != "NOISE"],
            key=lambda x: config.CATEGORY_ANCHORS[x]["priority"],
        )
        # Reset to 0
        db.reset_rotation()
        from main import get_rotated_order
        order0 = get_rotated_order()
        assert order0[0] == base[0]

        db.advance_rotation()
        order1 = get_rotated_order()
        assert order1[0] == base[1]

    def test_rotation_wraps(self):
        """After N advances, should cycle back to start."""
        import db
        import config
        from main import get_rotated_order

        db.reset_rotation()
        n = len([c for c in config.CATEGORY_ANCHORS if c != "NOISE"])
        for _ in range(n):
            db.advance_rotation()

        order = get_rotated_order()
        base = sorted(
            [c for c in config.CATEGORY_ANCHORS if c != "NOISE"],
            key=lambda x: config.CATEGORY_ANCHORS[x]["priority"],
        )
        assert order[0] == base[0]  # Should wrap around


# ── Dry-run pipeline integration test ─────────────────

class TestPipelineDryRun:
    def test_simulate_selection(self):
        from main import _simulate_selection
        import config

        articles = [
            {"title": f"Art {i}", "content_hash": f"h{i}",
             "category": cat, "score": float(i + 5),
             "link": f"https://example.com/{i}",
             "summary": "test summary"}
            for i, cat in enumerate(
                ["WELFARE", "ALERTS", "FINANCE", "TECH_SCI", "POLITICS",
                 "WAR_GEO", "WELFARE", "FINANCE"]
            )
        ]
        order = ["WELFARE", "ALERTS", "FINANCE", "TECH_SCI", "POLITICS", "WAR_GEO"]
        selected = _simulate_selection(articles, limit=4, priority_order=order)
        assert len(selected) <= 4

        # Check diversity: no category should repeat if avoidable
        cats = [a["category"] for a in selected]
        # First 4 slots should be from different categories (we have 6 categories)
        assert len(set(cats)) >= min(len(selected), 4)
