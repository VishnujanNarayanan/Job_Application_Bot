"""Embedding helpers — sentence-transformers (all-MiniLM-L6-v2, 384-dim).

Shared by Layer 2 (near-duplicate detection) and Layer 4 (scoring). The
model is loaded lazily and cached, so importing this module is cheap and
unit tests can run offline by monkeypatching ``embed`` / ``embed_batch``
or injecting vectors directly. ``cosine`` is pure math — testable with no
model present.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np

from src.config import settings

Vector = list[float]


@lru_cache(maxsize=1)
def _model():
    """Load and cache the sentence-transformers model (first call only)."""
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(settings.embeddings.model)


def embed(text: str) -> Vector:
    """Embed a single string into a 384-dim vector."""
    return _model().encode(text, normalize_embeddings=False).tolist()


def embed_batch(texts: list[str]) -> list[Vector]:
    """Embed many strings at once (more efficient than per-item calls)."""
    if not texts:
        return []
    return [v.tolist() for v in _model().encode(texts, normalize_embeddings=False)]


def add(a: Vector, b: Vector) -> Vector:
    """Element-wise sum of two vectors (architecture §4.1 jd_vec_match).

    Returns the other vector when one is empty. cosine() normalises later,
    so the un-normalised sum is fine as a query direction.
    """
    va = np.asarray(a, dtype=float)
    vb = np.asarray(b, dtype=float)
    if va.size == 0:
        return list(vb)
    if vb.size == 0:
        return list(va)
    return list(va + vb)


def cosine(a: Vector, b: Vector) -> float:
    """Cosine similarity in [-1, 1]; 0.0 if either vector is empty/zero."""
    va = np.asarray(a, dtype=float)
    vb = np.asarray(b, dtype=float)
    if va.size == 0 or vb.size == 0:
        return 0.0
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))
