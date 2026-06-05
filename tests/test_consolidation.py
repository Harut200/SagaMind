"""
SagaMind — Memory Consolidation Tests
=======================================

Tests the MemoryConsolidator's DBSCAN-style clustering and Neo4j
graph relationship writing.  All external stores are mocked.
"""

from unittest.mock import MagicMock

from src.memory.consolidation import MemoryConsolidator

# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _make_episode(memory_id: str, embedding: list, tenant_id: str = "t1"):
    """Create a dict-based episodic memory record."""
    return {
        "memory_id": memory_id,
        "tenant_id": tenant_id,
        "summary": f"Episode {memory_id}",
        "agent_role": "Planner",
        "embedding": embedding,
    }


def _mock_timescale(episodes):
    """Create a TimescaleMemoryStore-like mock with fallback_storage and get_all_memories."""
    ts = MagicMock()
    ts.fallback_storage = episodes
    ts.get_all_memories = MagicMock(return_value=episodes)
    return ts


# ─────────────────────────────────────────────────────────────────────
# Insufficient Memories
# ─────────────────────────────────────────────────────────────────────


class TestInsufficientMemories:
    """Consolidation skips when fewer than 2 memories are available."""

    def test_insufficient_memories_returns_zero_empty(self):
        ts = _mock_timescale([])
        neo = MagicMock()
        c = MemoryConsolidator(ts, neo)
        result = c.run_consolidation_cycle("t1")
        assert result == 0
        neo.upsert_relationship.assert_not_called()

    def test_insufficient_memories_returns_zero_single(self):
        ts = _mock_timescale([_make_episode("e1", [0.1, 0.2])])
        neo = MagicMock()
        c = MemoryConsolidator(ts, neo)
        result = c.run_consolidation_cycle("t1")
        assert result == 0

    def test_wrong_tenant_treated_as_empty(self):
        episodes = [_make_episode("e1", [0.1], tenant_id="other")]
        ts = _mock_timescale(episodes)
        neo = MagicMock()
        c = MemoryConsolidator(ts, neo)
        result = c.run_consolidation_cycle("t1")
        assert result == 0


# ─────────────────────────────────────────────────────────────────────
# Clustering
# ─────────────────────────────────────────────────────────────────────


class TestClustering:
    """Validate that similar embeddings are grouped into the same cluster."""

    def test_clustering_groups_similar_embeddings(self):
        """Two near-identical vectors should be clustered together."""
        episodes = [
            _make_episode("e1", [1.0, 0.0, 0.0]),
            _make_episode("e2", [0.99, 0.01, 0.0]),  # very close to e1
            _make_episode("e3", [0.0, 0.0, 1.0]),  # far from e1/e2
        ]
        ts = _mock_timescale(episodes)
        neo = MagicMock()
        c = MemoryConsolidator(ts, neo)

        num_clusters = c.run_consolidation_cycle("t1", eps=0.2)
        # We expect at least 1 cluster (e1+e2), e3 may be isolated
        assert num_clusters >= 1

    def test_all_identical_vectors_single_cluster(self):
        """Identical vectors should form a single cluster."""
        episodes = [_make_episode(f"e{i}", [0.5, 0.5, 0.5]) for i in range(5)]
        ts = _mock_timescale(episodes)
        neo = MagicMock()
        c = MemoryConsolidator(ts, neo)

        num_clusters = c.run_consolidation_cycle("t1", eps=0.2)
        assert num_clusters == 1

    def test_distant_vectors_separate_clusters(self):
        """Orthogonal vectors with eps=0.05 should form separate clusters."""
        episodes = [
            _make_episode("e1", [1.0, 0.0, 0.0]),
            _make_episode("e2", [0.0, 1.0, 0.0]),
            _make_episode("e3", [0.0, 0.0, 1.0]),
        ]
        ts = _mock_timescale(episodes)
        neo = MagicMock()
        c = MemoryConsolidator(ts, neo)

        num_clusters = c.run_consolidation_cycle("t1", eps=0.05)
        # Each vector is far from the others; all isolated → 3 clusters
        assert num_clusters == 3


# ─────────────────────────────────────────────────────────────────────
# Graph Relationship Writing
# ─────────────────────────────────────────────────────────────────────


class TestGraphRelationships:
    """Verify that Neo4j upsert_relationship is called for clustered items."""

    def test_graph_relationships_written(self):
        """When a cluster has ≥2 items, upsert_relationship must be called."""
        episodes = [
            _make_episode("e1", [1.0, 0.0, 0.0]),
            _make_episode("e2", [0.99, 0.01, 0.0]),
        ]
        ts = _mock_timescale(episodes)
        neo = MagicMock()
        c = MemoryConsolidator(ts, neo)

        c.run_consolidation_cycle("t1", eps=0.2)
        assert neo.upsert_relationship.call_count > 0

    def test_relationship_types_correct(self):
        """Verify the relation types are SUMMARIZES_EXPERIENCE and DISCOVERED_CONCEPT."""
        episodes = [
            _make_episode("e1", [1.0, 0.0]),
            _make_episode("e2", [0.99, 0.01]),
        ]
        ts = _mock_timescale(episodes)
        neo = MagicMock()
        c = MemoryConsolidator(ts, neo)

        c.run_consolidation_cycle("t1", eps=0.3)

        relation_types = {
            c_call[1]["relation"] if "relation" in c_call[1] else c_call[0][1]
            for c_call in neo.upsert_relationship.call_args_list
        }
        assert "SUMMARIZES_EXPERIENCE" in relation_types
        assert "DISCOVERED_CONCEPT" in relation_types

    def test_no_relationships_for_singletons(self):
        """Isolated single-element clusters should not produce graph edges."""
        episodes = [
            _make_episode("e1", [1.0, 0.0, 0.0]),
            _make_episode("e2", [0.0, 1.0, 0.0]),
        ]
        ts = _mock_timescale(episodes)
        neo = MagicMock()
        c = MemoryConsolidator(ts, neo)

        c.run_consolidation_cycle("t1", eps=0.01)  # very tight eps
        # With orthogonal vectors and eps=0.01, no cluster has ≥2 members
        neo.upsert_relationship.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# Cosine Distance Helper
# ─────────────────────────────────────────────────────────────────────


class TestCosineDistance:
    """Unit-test the internal cosine distance computation."""

    def test_identical_vectors_zero_distance(self):
        c = MemoryConsolidator(MagicMock(), MagicMock())
        d = c.compute_cosine_distance([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
        assert abs(d) < 1e-9

    def test_orthogonal_vectors_distance_one(self):
        c = MemoryConsolidator(MagicMock(), MagicMock())
        d = c.compute_cosine_distance([1.0, 0.0], [0.0, 1.0])
        assert abs(d - 1.0) < 1e-9

    def test_zero_vector_returns_one(self):
        c = MemoryConsolidator(MagicMock(), MagicMock())
        d = c.compute_cosine_distance([0.0, 0.0], [1.0, 2.0])
        assert d == 1.0


# ─────────────────────────────────────────────────────────────────────
# Database Query Routing
# ─────────────────────────────────────────────────────────────────────


class TestConsolidationDatabaseRoute:
    """Verifies that the consolidation cycle uses the get_all_memories method if available."""

    def test_database_fetch_called(self):
        ts = MagicMock()
        # Ensure fallback_storage is not present on the mock to test the get_all_memories branch
        if hasattr(ts, "fallback_storage"):
            del ts.fallback_storage
        ts.get_all_memories = MagicMock(
            return_value=[_make_episode("e1", [1.0, 0.0]), _make_episode("e2", [0.99, 0.01])]
        )

        neo = MagicMock()
        c = MemoryConsolidator(ts, neo)
        c.run_consolidation_cycle("t1", eps=0.2)

        ts.get_all_memories.assert_called_once_with("t1")
        assert neo.upsert_relationship.call_count > 0
