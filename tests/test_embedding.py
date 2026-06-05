"""
SagaMind — Embedding Service Tests
==================================

Validates the deterministic offline fallback used when no OpenAI key is configured.
"""

import math

from src.memory.embedding import EmbeddingService


class TestDeterministicEmbedding:
    def test_dimensionality(self):
        es = EmbeddingService(dim=64)
        assert len(es.embed("hello world")) == 64

    def test_unit_norm(self):
        es = EmbeddingService(dim=128)
        v = es.embed("a memory")
        norm = math.sqrt(sum(x * x for x in v))
        assert abs(norm - 1.0) < 1e-6

    def test_deterministic_for_same_text(self):
        es = EmbeddingService(dim=32)
        assert es.embed("repeatable") == es.embed("repeatable")

    def test_distinct_for_different_text(self):
        es = EmbeddingService(dim=32)
        assert es.embed("alpha") != es.embed("beta")

    def test_batch(self):
        es = EmbeddingService(dim=16)
        out = es.embed_batch(["x", "y", "z"])
        assert len(out) == 3
        assert all(len(v) == 16 for v in out)
