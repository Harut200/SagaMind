"""
SagaMind Embedding Service
==========================

Produces dense vector embeddings for memory text. Uses the configured OpenAI model when
an API key is available, and otherwise a **deterministic** hash-seeded fallback so that
vector search is exercised identically across runs, offline and in tests.

Caching
-------
Results are cached in an LRU cache keyed on ``(text, model)``.  Identical queries within
the same process never hit the OpenAI API twice.  The cache is bounded to 4 096 entries
(~6 MB at 1 536 dims × 4 bytes × 4 096 ≈ 24 MB peak — acceptable for an API server).
"""

from __future__ import annotations

import hashlib
import logging
from functools import lru_cache

from src.config import settings

logger = logging.getLogger("SagaMind.Memory.Embedding")


class EmbeddingService:
    """Text → vector encoder with LRU cache and deterministic offline fallback."""

    def __init__(self, dim: int | None = None) -> None:
        self.dim = dim or settings.embedding_dim
        self.model = settings.embedding_model
        self._client = None
        if settings.openai_api_key:
            try:
                import openai

                self._client = openai.OpenAI(api_key=settings.openai_api_key)
                logger.info("OpenAI embedding client initialised (model=%s).", self.model)
            except Exception as exc:  # noqa: BLE001 - optional dependency
                logger.warning("OpenAI client unavailable, using deterministic fallback: %s", exc)

    def embed(self, text: str) -> list[float]:
        """Return a unit-norm embedding vector for *text* (LRU-cached per process)."""
        return list(_cached_embed(text, self.model, self.dim, self._client))

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    def _deterministic_embedding(self, text: str, dim: int) -> list[float]:
        seed = hashlib.sha256(text.encode("utf-8")).digest()
        raw = bytearray()
        counter = 0
        while len(raw) < dim * 2:
            raw.extend(hashlib.sha256(seed + counter.to_bytes(4, "big")).digest())
            counter += 1
        values = [(int.from_bytes(raw[i : i + 2], "big") / 65535.0) - 0.5 for i in range(0, dim * 2, 2)]
        norm = sum(v * v for v in values) ** 0.5 or 1.0
        return [v / norm for v in values]


@lru_cache(maxsize=4096)
def _cached_embed(text: str, model: str, dim: int, client: object) -> tuple[float, ...]:
    """Module-level cached embed so the cache survives across ``EmbeddingService`` instances."""
    if client is not None:
        try:
            import openai

            if isinstance(client, openai.OpenAI):
                resp = client.embeddings.create(model=model, input=text)
                return tuple(resp.data[0].embedding)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Embedding API call failed, using fallback: %s", exc)
    return tuple(_deterministic_embed(text, dim))


def _deterministic_embed(text: str, dim: int) -> list[float]:
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    raw = bytearray()
    counter = 0
    while len(raw) < dim * 2:
        raw.extend(hashlib.sha256(seed + counter.to_bytes(4, "big")).digest())
        counter += 1
    values = [(int.from_bytes(raw[i : i + 2], "big") / 65535.0) - 0.5 for i in range(0, dim * 2, 2)]
    norm = sum(v * v for v in values) ** 0.5 or 1.0
    return [v / norm for v in values]
