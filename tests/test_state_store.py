"""
SagaMind — Saga State Store Tests (in-memory backend)
=====================================================

Exercises the durable-store interface in its offline fallback mode (no Postgres/Redis),
which is the contract the coordinator depends on.
"""

from src.orchestrator.state_store import SagaStateStore


class TestBackendSelection:
    def test_defaults_to_memory_offline(self):
        store = SagaStateStore()
        assert store.backend == "memory"


class TestStateLifecycle:
    def test_running_saga_is_incomplete(self):
        store = SagaStateStore()
        store.write_transaction_state("s1", "RUNNING", {"goal": "g", "tenant_id": "t1"})
        ids = {s["saga_id"] for s in store.list_incomplete()}
        assert "s1" in ids

    def test_committed_saga_is_not_incomplete(self):
        store = SagaStateStore()
        store.write_transaction_state("s1", "RUNNING", {})
        store.write_transaction_state("s1", "COMMITTED", {})
        ids = {s["saga_id"] for s in store.list_incomplete()}
        assert "s1" not in ids

    def test_rolled_back_is_terminal(self):
        store = SagaStateStore()
        store.write_transaction_state("s2", "RUNNING", {})
        store.write_transaction_state("s2", "ROLLED_BACK", {})
        assert all(s["saga_id"] != "s2" for s in store.list_incomplete())


class TestCompensationLog:
    def test_compensations_recorded_in_order(self):
        store = SagaStateStore()
        store.write_transaction_state("s3", "RUNNING", {})
        store.append_compensation("s3", "DELETE_FILE", {"path": "/a"})
        store.append_compensation("s3", "DATABASE_QUERY", {"q": "DELETE 1"})
        incomplete = {s["saga_id"]: s for s in store.list_incomplete()}
        comps = incomplete["s3"]["compensations"]
        assert [c["tool_name"] for c in comps] == ["DELETE_FILE", "DATABASE_QUERY"]
