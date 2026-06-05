"""
Live-backend integration tests (skipped unless RUN_INTEGRATION=1).

Assumes the services from docker-compose.yml are reachable using the environment's DB/Neo4j
settings. These assert the real persistence paths that the unit suite can only mock.
"""

import uuid

import pytest

from src.memory.neo4j_store import Neo4jGraphStore
from src.memory.timescale_store import TimescaleMemoryStore
from src.orchestrator.state_store import SagaStateStore

pytestmark = pytest.mark.integration


class TestTimescaleLive:
    def test_write_and_retrieve(self):
        store = TimescaleMemoryStore()
        assert store.pool_active, "TimescaleDB must be reachable for integration tests"
        tenant = f"t-{uuid.uuid4().hex[:8]}"
        store.write_episodic_memory(
            memory_id=str(uuid.uuid4()),
            tenant_id=tenant,
            agent_role="Planner",
            summary="integration write",
            importance=0.8,
            embedding=[0.01] * 1536,
        )
        rows = store.get_all_memories(tenant)
        assert len(rows) == 1
        assert rows[0]["summary"] == "integration write"


class TestNeo4jLive:
    def test_upsert_and_read_back(self):
        graph = Neo4jGraphStore()
        assert graph.active, "Neo4j must be reachable for integration tests"
        graph.upsert_relationship("ConceptA", "RELATES_TO", "ConceptB", weight=0.9)
        neighbors = graph.get_neighbors("ConceptA")
        assert any(n["target"] == "ConceptB" for n in neighbors)


class TestSagaStateLive:
    def test_durable_state_and_recovery_log(self):
        store = SagaStateStore()
        assert store.backend in {"postgres", "redis"}, "a durable saga backend is required"
        saga_id = str(uuid.uuid4())
        store.write_transaction_state(saga_id, "RUNNING", {"tenant_id": "t1", "goal": "g"})
        store.append_compensation(saga_id, "DELETE_FILE", {"path": "/app/workspace/x"})
        incomplete = {s["saga_id"] for s in store.list_incomplete()}
        assert saga_id in incomplete
        store.write_transaction_state(saga_id, "COMMITTED", {})
        incomplete_after = {s["saga_id"] for s in store.list_incomplete()}
        assert saga_id not in incomplete_after
