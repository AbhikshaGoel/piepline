"""
ai.py - Two-layer AI classification.
  Layer 1: Local SentenceTransformer (all-MiniLM-L6-v2) 
  Layer 2: Regex pattern matching (ultimate fallback)
No cloud APIs, no rate limit costs, runs fully offline.
"""
import re
import logging
import numpy as np
from typing import List, Dict, Tuple, Optional

import config

log = logging.getLogger(__name__)


# ── Layer 1: Local Embeddings ─────────────────────────

class LocalEmbedding:
    """SentenceTransformer wrapper with lazy model loading."""

    def __init__(self):
        self._model = None
        self._model_name = None
        self._available = False
        self._check_available()

    def _check_available(self):
        try:
            from sentence_transformers import SentenceTransformer  # noqa
            self._ST = SentenceTransformer
            self._available = True
            log.info("✅ SentenceTransformers available")
        except ImportError:
            log.warning("⚠️  sentence-transformers not installed → regex-only mode")

    def _load(self, model_name: str):
        if self._model_name != model_name or self._model is None:
            log.info(f"📥 Loading model: {model_name} ...")
            self._model = self._ST(model_name)
            self._model_name = model_name
            log.info(f"✅ Model loaded: {model_name}")

    def encode(self, texts: List[str]) -> Optional[List[List[float]]]:
        if not self._available or not texts:
            return None
        cfg = config.LOCAL_AI_CONFIG
        # Try primary, then fallback model
        for model in [cfg["primary_model"], cfg.get("fallback_model")]:
            if not model:
                continue
            try:
                self._load(model)
                vecs = self._model.encode(
                    texts,
                    batch_size=cfg["batch_size"],
                    show_progress_bar=cfg["show_progress"],
                    convert_to_numpy=True,
                    normalize_embeddings=True,  # Pre-normalize → faster cosine
                )
                log.info(f"✅ Embedded {len(vecs)} texts via {model}")
                return vecs.tolist()
            except Exception as e:
                log.warning(f"⚠️  Model {model} failed: {e}")
        return None

    @property
    def available(self) -> bool:
        return self._available


# ── Layer 2: Regex Classifier ─────────────────────────

class RegexClassifier:
    """Fallback classifier. Pre-compiles all patterns at startup."""

    def __init__(self):
        self._patterns = {
            cat: [re.compile(p, re.IGNORECASE) for p in pats]
            for cat, pats in config.REGEX_FALLBACK.items()
        }

    def classify(self, text: str) -> Tuple[str, float]:
        """Returns (category, confidence 0-1)."""
        scores = {
            cat: sum(1 for p in pats if p.search(text))
            for cat, pats in self._patterns.items()
        }
        best = max(scores, key=scores.get)
        hits = scores[best]
        if hits == 0:
            return "GENERAL", 0.2
        return best, min(hits / 3.0, 1.0)


# ── Cosine similarity (numpy) ─────────────────────────

def _cosine(v1: List[float], v2: List[float]) -> float:
    a, b = np.array(v1), np.array(v2)
    n1, n2 = np.linalg.norm(a), np.linalg.norm(b)
    if n1 == 0 or n2 == 0:
        return 0.0
    return float(np.dot(a, b) / (n1 * n2))


# ── Main AI Engine ────────────────────────────────────

class AIEngine:
    """
    Orchestrates embedding + regex classification.
    Pre-computes category anchor embeddings once at startup.
    """

    def __init__(self):
        self._local  = LocalEmbedding()
        self._regex  = RegexClassifier()
        self._anchors: Dict[str, List[float]] = {}
        self._anchor_method = "regex"
        self._init_anchors()

    def _init_anchors(self):
        """Build anchor vector for each category description."""
        descs      = [v["desc"] for v in config.CATEGORY_ANCHORS.values()]
        categories = list(config.CATEGORY_ANCHORS.keys())

        log.info("🧠 Computing category anchor embeddings...")
        vecs = self._local.encode(descs)

        if vecs:
            self._anchors = dict(zip(categories, vecs))
            self._anchor_method = "local"
            log.info(f"✅ {len(self._anchors)} anchors ready (local AI)")
        else:
            self._anchor_method = "regex"
            log.warning("⚠️  No anchor embeddings — regex-only classification")

    def _classify_by_embedding(
        self, embedding: List[float]
    ) -> Tuple[str, float, str]:
        """Returns (category, score, method='embedding')."""
        best_cat, best_sim = "GENERAL", -1.0
        for cat, anchor in self._anchors.items():
            sim = _cosine(embedding, anchor)
            if sim > best_sim:
                best_sim, best_cat = sim, cat

        weight = config.CATEGORY_ANCHORS.get(best_cat, {}).get("weight", 0)
        score  = -50.0 if best_cat == "NOISE" else (best_sim * 10.0) + weight
        return best_cat, round(score, 2), "embedding"

    def _classify_by_regex(self, text: str) -> Tuple[str, float, str]:
        """Returns (category, score, method='regex')."""
        cat, conf = self._regex.classify(text)
        weight = config.CATEGORY_ANCHORS.get(cat, {}).get("weight", 0)
        score  = -50.0 if cat == "NOISE" else 5.0 + weight + (conf * 5.0)
        return cat, round(score, 2), "regex"

    def process_articles(self, articles: List[Dict]) -> List[Dict]:
        """
        Classify and score all articles in one batch call.
        Attaches: category, score, embedding, classification_method
        """
        if not articles:
            return []

        log.info(f"🧠 Processing {len(articles)} articles...")
        texts = [f"{a.get('title','')} {a.get('summary','')}" for a in articles]

        # Batch embed
        embeddings = None
        if self._anchor_method == "local":
            embeddings = self._local.encode(texts)

        method_counts: Dict[str, int] = {}

        for i, article in enumerate(articles):
            emb = embeddings[i] if embeddings and i < len(embeddings) else None

            if emb is not None and self._anchors:
                cat, score, method = self._classify_by_embedding(emb)
            else:
                cat, score, method = self._classify_by_regex(texts[i])
                emb = None  # don't store None embeddings in Chroma

            article["category"]               = cat
            article["score"]                  = score
            article["embedding"]              = emb
            article["classification_method"]  = method
            method_counts[method] = method_counts.get(method, 0) + 1

        log.info(f"✅ Done. Methods used: {method_counts}")
        return articles
