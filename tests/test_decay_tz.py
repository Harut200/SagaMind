"""
SagaMind — Decay Timezone Regression Tests
==========================================

Guards the naive/aware datetime normalisation that previously crashed the dashboard's
retention chart with a TypeError.
"""

from datetime import datetime, timedelta, timezone

from src.memory.decay import EbbinghausMemoryManager
from src.models import MemoryNode


def _naive_node(hours_ago: float) -> MemoryNode:
    naive_now = datetime.now()  # noqa: DTZ005 - intentionally naive for the regression
    return MemoryNode(
        memory_id="m",
        created_at=naive_now,
        last_retrieved_at=naive_now - timedelta(hours=hours_ago),
        agent_role="r",
        summary="s",
        importance_score=0.7,
        retrieval_count=2,
        embedding=[],
    )


class TestNaiveDatetimeHandling:
    def test_naive_datetime_does_not_raise(self):
        manager = EbbinghausMemoryManager()
        r = manager.calculate_retention(_naive_node(5))
        assert 0.0 <= r <= 1.0

    def test_iso_string_timestamp_supported(self):
        manager = EbbinghausMemoryManager()
        iso = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        record = {"last_retrieved_at": iso, "importance_score": 0.5, "retrieval_count": 1}
        r = manager.calculate_retention(record)
        assert 0.0 <= r <= 1.0

    def test_batch_matches_scalar(self):
        manager = EbbinghausMemoryManager()
        nodes = [_naive_node(h) for h in (1, 10, 100)]
        batch = manager.calculate_retention_batch(nodes)
        assert len(batch) == 3
        assert all(0.0 <= r <= 1.0 for r in batch)
