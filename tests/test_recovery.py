"""
SagaMind — Crash Recovery Tests
===============================

Validates that the coordinator replays persisted compensations (LIFO) for sagas left
incomplete by a previous process, using the in-memory durable store.
"""

from unittest.mock import MagicMock

from src.orchestrator.coordinator import SagaTransactionCoordinator
from src.orchestrator.state_store import SagaStateStore


class TestRecover:
    def test_recover_replays_compensations_lifo(self):
        store = SagaStateStore()
        store.write_transaction_state("s1", "RUNNING", {})
        store.append_compensation("s1", "DELETE_FILE", {"path": "/a"})
        store.append_compensation("s1", "DATABASE_QUERY", {"q": "DELETE 1"})

        sandbox = MagicMock()
        sandbox.execute_compensation = MagicMock(return_value=True)
        coord = SagaTransactionCoordinator(MagicMock(), sandbox, db_client=store)

        recovered = coord.recover()

        assert recovered == 1
        assert sandbox.execute_compensation.call_count == 2
        order = [c.args[0].tool_name for c in sandbox.execute_compensation.call_args_list]
        assert order == ["DATABASE_QUERY", "DELETE_FILE"]  # reverse of append order
        # The saga is now terminal and no longer eligible for recovery.
        assert all(s["saga_id"] != "s1" for s in store.list_incomplete())

    def test_recover_noop_without_db(self):
        coord = SagaTransactionCoordinator(MagicMock(), MagicMock())
        assert coord.recover() == 0

    def test_execute_saga_persists_state_when_db_wired(self):
        store = SagaStateStore()
        verifier = MagicMock()
        verifier.verify = MagicMock(return_value=(True, "OK"))
        sandbox = MagicMock()
        sandbox.execute = MagicMock(return_value={"status": "SUCCESS"})

        from src.models import ActionPayload, SagaStep

        coord = SagaTransactionCoordinator(verifier, sandbox, db_client=store)
        step = SagaStep(
            step_id="x",
            step_name="write",
            action=ActionPayload("WRITE_FILE", {"path": "/p"}),
            compensation=ActionPayload("DELETE_FILE", {"path": "/p"}),
            invariants="",
        )
        assert coord.execute_saga("saga-d", [step]) is True
        # Durable store reflects the terminal COMMITTED state.
        assert all(s["saga_id"] != "saga-d" for s in store.list_incomplete())
