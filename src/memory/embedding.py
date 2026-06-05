"""
SagaMind Embedding Service
==========================

Produces dense vector embeddings for memory text. Uses the configured OpenAI model when
an API key is available, and otherwise a **deterministic** hash-seeded fallback so that
vector search is exercised identically across runs, offline and in tests (the previous
code never generated embeddings at all and queried with a constant dummy vector).
"""

from __future__ import annotations

import hashlib
import logging

from src.config import settings

logger = logging.getLogger("SagaMind.Memory.Embedding")


class EmbeddingService:
    """Text → vector encoder with a graceful deterministic fallback."""

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
        """Return a unit-norm embedding vector for *text*."""
        if self._client is not None:
            try:
                resp = self._client.embeddings.create(model=self.model, input=text)
                return list(resp.data[0].embedding)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Embedding API call failed, using fallback: %s", exc)
        return self._deterministic_embedding(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    def _deterministic_embedding(self, text: str) -> list[float]:
        """Hash-seeded pseudo-random unit vector — stable for a given input string."""
        seed = hashlib.sha256(text.encode("utf-8")).digest()
        # Expand the digest deterministically to the target dimensionality.
        raw = bytearray()
        counter = 0
        while len(raw) < self.dim * 2:
            raw.extend(hashlib.sha256(seed + counter.to_bytes(4, "big")).digest())
            counter += 1
        values = [(int.from_bytes(raw[i : i + 2], "big") / 65535.0) - 0.5 for i in range(0, self.dim * 2, 2)]
        norm = sum(v * v for v in values) ** 0.5 or 1.0
        return [v / norm for v in values]
