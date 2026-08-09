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


# all-MiniLM-L6-v2 accepts 256 word-pieces and silently discards the rest, so
# a single encode() of a job ad reads roughly its first 1,200 characters and
# throws the remainder away. Measured on real listings: embed(whole JD) is
# bit-identical to embed(first 1,200 chars), cosine 1.0000.
#
# That matters because the median ad here is 4,172 characters and pay is first
# mentioned a median 77% of the way in. Near-duplicate detection was therefore
# comparing openings — and openings are boilerplate, which is precisely where
# two different roles at one company look identical.
#
# Chunk, embed each piece, and average by length: every part of the document
# gets a say, weighted by how much of the document it is.
_CHARS_PER_CHUNK = 1000     # comfortably inside the window, with overlap room
_CHUNK_OVERLAP = 150        # so a sentence split across chunks still lands whole


def _chunks(text: str, size: int, overlap: int) -> list[str]:
    """Split into overlapping windows, preferring a nearby line break."""
    if len(text) <= size:
        return [text]

    out: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            # Break on a newline in the last quarter, so chunks land on
            # section boundaries (JDs are heavily bulleted) rather than
            # mid-word.
            pivot = text.rfind("\n", start + (size * 3 // 4), end)
            if pivot > start:
                end = pivot
        out.append(text[start:end])
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return out


def embed_document(text: str) -> Vector:
    """Embed a whole document, including everything past the model's window.

    Length-weighted mean of the chunk vectors, which approximates encoding the
    full text: a 4,000-character ad contributes all of itself instead of its
    first 1,200 characters.

    Short texts take the fast path and are byte-identical to :func:`embed`, so
    nothing that already fits changes value.
    """
    if not text:
        return []

    pieces = _chunks(text, _CHARS_PER_CHUNK, _CHUNK_OVERLAP)
    if len(pieces) == 1:
        return embed(text)

    vectors = np.asarray(embed_batch(pieces), dtype=float)
    weights = np.asarray([len(p) for p in pieces], dtype=float)
    return list(np.average(vectors, axis=0, weights=weights))


def embed_documents(texts: list[str]) -> list[Vector]:
    """:func:`embed_document` for many texts, batching the short ones."""
    return [embed_document(t) for t in texts]


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
