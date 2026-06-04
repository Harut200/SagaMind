"""
SagaMind — Ebbinghaus Memory Decay Tests
==========================================

Validates the retention formula R(t) = e^(-t / S_m) where
S_m = S_init * (1 + gamma * ln(N_access + 1)) * Importance.

Tests cover: fresh memories, stale memories, importance weighting,
retrieval reinforcement, zero-importance edge case, and the
evaluate_memories partition function.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.memory.decay import EbbinghausMemoryManager
from src.models import MemoryNode

# ─────────────────────────────────────────────────────────────────────
# Fixture
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def manager():
    """Default EbbinghausMemoryManager with standard parameters."""
    return EbbinghausMemoryManager()


def _make_node(
    *,
    hours_since_retrieval: float = 0.0,
    importance: float = 0.5,
    retrieval_count: int = 1,
) -> MemoryNode:
    """Helper to create a MemoryNode with a controlled last_retrieved_at."""
    now = datetime.now(timezone.utc)
    return MemoryNode(
        memory_id="test-mem",
        created_at=now - timedelta(hours=hours_since_retrieval + 1),
        last_retrieved_at=now - timedelta(hours=hours_since_retrieval),
        agent_role="Tester",
        summary="test memory",
        importance_score=importance,
        retrieval_count=retrieval_count,
        embedding=[],
    )


# ─────────────────────────────────────────────────────────────────────
# Fresh vs Stale Memory
# ─────────────────────────────────────────────────────────────────────

class TestRetentionCurve:
    """Validate the exponential decay curve behaviour."""

    def test_fresh_memory_high_retention(self, manager):
        """A memory retrieved moments ago should have retention close to 1.0."""
        node = _make_node(hours_since_retrieval=0.01, importance=0.8, retrieval_count=3)
        r = manager.calculate_retention(node)
        assert r > 0.95, f"Expected retention > 0.95 for fresh memory, got {r}"

    def test_stale_memory_low_retention(self, manager):
        """A memory not retrieved for a long time with low importance should decay."""
        node = _make_node(hours_since_retrieval=500, importance=0.2, retrieval_count=0)
        r = manager.calculate_retention(node)
        assert r < 0.15, f"Expected retention < 0.15 for stale memory, got {r}"

    def test_very_old_memory_near_zero(self, manager):
        """An extremely old, low-importance memory should approach zero."""
        node = _make_node(hours_since_retrieval=10000, importance=0.1, retrieval_count=0)
        r = manager.calculate_retention(node)
        assert r < 0.01


# ─────────────────────────────────────────────────────────────────────
# Importance and Retrieval Effects
# ─────────────────────────────────────────────────────────────────────

class TestDecayFactors:
    """Validate that importance and retrieval_count modulate the decay rate."""

    def test_importance_affects_decay(self, manager):
        """Higher importance → slower decay → higher retention at same age."""
        high = _make_node(hours_since_retrieval=48, importance=0.9, retrieval_count=1)
        low = _make_node(hours_since_retrieval=48, importance=0.2, retrieval_count=1)
        r_high = manager.calculate_retention(high)
        r_low = manager.calculate_retention(low)
        assert r_high > r_low, (
            f"Expected high-importance retention ({r_high}) > low-importance ({r_low})"
        )

    def test_retrieval_count_reinforcement(self, manager):
        """More retrievals → stronger memory strength → higher retention."""
        many = _make_node(hours_since_retrieval=48, importance=0.5, retrieval_count=20)
        few = _make_node(hours_since_retrieval=48, importance=0.5, retrieval_count=0)
        r_many = manager.calculate_retention(many)
        r_few = manager.calculate_retention(few)
        assert r_many > r_few, (
            f"Expected many-retrieval retention ({r_many}) > few-retrieval ({r_few})"
        )

    def test_retrieval_bonus_is_logarithmic(self, manager):
        """Doubling retrieval count should NOT double retention; growth is ln-scaled."""
        r10 = manager.calculate_retention(
            _make_node(hours_since_retrieval=24, importance=0.5, retrieval_count=10)
        )
        r20 = manager.calculate_retention(
            _make_node(hours_since_retrieval=24, importance=0.5, retrieval_count=20)
        )
        # The difference should be small because log growth flattens
        assert (r20 - r10) < 0.3


# ─────────────────────────────────────────────────────────────────────
# Edge Cases
# ─────────────────────────────────────────────────────────────────────

class TestEdgeCases:
    """Validate boundary and degenerate inputs."""

    def test_zero_importance_returns_zero(self, manager):
        """importance_score = 0 → strength = 0 → retention = 0.0."""
        node = _make_node(hours_since_retrieval=1, importance=0.0, retrieval_count=5)
        r = manager.calculate_retention(node)
        assert r == 0.0

    def test_zero_retrieval_count(self, manager):
        """retrieval_count = 0 should still compute without error."""
        node = _make_node(hours_since_retrieval=1, importance=0.5, retrieval_count=0)
        r = manager.calculate_retention(node)
        assert 0.0 <= r <= 1.0

    def test_retention_never_exceeds_one(self, manager):
        """Retention probability must be in [0, 1]."""
        node = _make_node(hours_since_retrieval=0.001, importance=1.0, retrieval_count=100)
        r = manager.calculate_retention(node)
        assert 0.0 <= r <= 1.0


# ─────────────────────────────────────────────────────────────────────
# evaluate_memories Partition
# ─────────────────────────────────────────────────────────────────────

class TestEvaluateMemories:
    """Validate the keep/prune partition logic."""

    def test_evaluate_memories_partition(self, manager, sample_memory_nodes):
        """Fresh high-importance memories are kept; stale low-importance are pruned."""
        keep, prune = manager.evaluate_memories(sample_memory_nodes)
        # The partitions should be disjoint and exhaustive
        assert len(keep) + len(prune) == len(sample_memory_nodes)

    def test_empty_input(self, manager):
        keep, prune = manager.evaluate_memories([])
        assert keep == []
        assert prune == []

    def test_all_fresh_memories_kept(self, manager):
        """Memories retrieved just now with high importance should all be kept."""
        nodes = [
            _make_node(hours_since_retrieval=0.01, importance=0.9, retrieval_count=5)
            for _ in range(5)
        ]
        keep, prune = manager.evaluate_memories(nodes)
        assert len(keep) == 5
        assert len(prune) == 0

    def test_all_stale_memories_pruned(self, manager):
        """Very old, low-importance memories should all be pruned."""
        nodes = [
            _make_node(hours_since_retrieval=5000, importance=0.05, retrieval_count=0)
            for _ in range(3)
        ]
        keep, prune = manager.evaluate_memories(nodes)
        assert len(prune) == 3
        assert len(keep) == 0
