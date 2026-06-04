"""
SagaMind — Saga Transaction Coordinator Tests
===============================================

Tests the full saga lifecycle: transaction logging, step execution,
LIFO compensations on verification failure / runtime exception,
compensation failure handling, and callback invocation.
"""

import pytest
from unittest.mock import MagicMock, call

from src.models import ActionPayload, SagaStep, StepStatus


# ─────────────────────────────────────────────────────────────────────
# Transaction Initialization
# ─────────────────────────────────────────────────────────────────────

class TestStartTransaction:
    """Validate start_transaction_log creates the correct saga entry."""

    def test_start_transaction_creates_saga(self, coordinator):
        coordinator.start_transaction_log("saga-100", "Deploy service", "tenant-A")

        assert "saga-100" in coordinator.active_sagas
        saga = coordinator.active_sagas["saga-100"]
        assert saga["saga_id"] == "saga-100"
        assert saga["tenant_id"] == "tenant-A"
        assert saga["goal"] == "Deploy service"
        assert saga["status"] == "RUNNING"
        assert saga["completed_steps"] == []
        assert saga["start_time"] > 0

    def test_start_transaction_idempotent_overwrite(self, coordinator):
        """Calling start twice overwrites the previous saga entry."""
        coordinator.start_transaction_log("saga-200", "goal-1", "t1")
        coordinator.start_transaction_log("saga-200", "goal-2", "t2")
        assert coordinator.active_sagas["saga-200"]["goal"] == "goal-2"


# ─────────────────────────────────────────────────────────────────────
# Successful Execution
# ─────────────────────────────────────────────────────────────────────

class TestExecuteSagaSuccess:
    """Validate that all steps commit when verifier and sandbox both pass."""

    def test_execute_saga_success(self, coordinator, mock_sandbox, sample_steps):
        coordinator.start_transaction_log("saga-ok", "happy path", "tenant-1")
        result = coordinator.execute_saga("saga-ok", sample_steps)

        assert result is True
        assert coordinator.active_sagas["saga-ok"]["status"] == "COMMITTED"
        # Every step should have been committed
        for step in sample_steps:
            assert step.status == "COMMITTED"
        # Sandbox.execute was called once per step
        assert mock_sandbox.execute.call_count == len(sample_steps)
        # No compensations should have fired
        mock_sandbox.execute_compensation.assert_not_called()

    def test_auto_creates_saga_if_missing(self, coordinator, sample_steps):
        """execute_saga auto-initialises a saga when the id is unknown."""
        result = coordinator.execute_saga("saga-auto", sample_steps)
        assert result is True
        assert "saga-auto" in coordinator.active_sagas


# ─────────────────────────────────────────────────────────────────────
# Rollback on Verification Failure
# ─────────────────────────────────────────────────────────────────────

class TestRollbackOnVerificationFailure:
    """Validate LIFO compensations when the verifier rejects a step."""

    def test_execute_saga_rollback_on_verification_failure(
        self, failing_verifier, mock_sandbox
    ):
        from src.orchestrator.coordinator import SagaTransactionCoordinator

        coord = SagaTransactionCoordinator(failing_verifier, mock_sandbox)
        coord.start_transaction_log("saga-vf", "verify fail", "t1")

        steps = [
            SagaStep(
                step_id="s1", step_name="step-1",
                action=ActionPayload(tool_name="T", arguments={}),
                compensation=ActionPayload(tool_name="C", arguments={}),
                invariants="x",
            ),
        ]

        result = coord.execute_saga("saga-vf", steps)
        assert result is False
        assert steps[0].status == "FAILED"
        assert "Logic Solver check failed" in steps[0].error

    def test_lifo_compensation_order(self, mock_sandbox):
        """
        Steps 1 and 2 succeed (verifier passes), step 3 fails (verifier
        rejects). Compensations for steps 2 then 1 should fire in LIFO order.
        """
        from src.orchestrator.coordinator import SagaTransactionCoordinator

        call_order = []

        verifier = MagicMock()
        # First two calls pass, third fails
        verifier.verify = MagicMock(
            side_effect=[(True, "OK"), (True, "OK"), (False, "Violation")]
        )

        def track_compensation(comp):
            call_order.append(comp.tool_name)
            return True

        mock_sandbox.execute_compensation = MagicMock(side_effect=track_compensation)

        coord = SagaTransactionCoordinator(verifier, mock_sandbox)
        coord.start_transaction_log("saga-lifo", "lifo test", "t1")

        steps = [
            SagaStep(
                step_id=f"s{i}", step_name=f"step-{i}",
                action=ActionPayload(tool_name=f"T{i}", arguments={}),
                compensation=ActionPayload(tool_name=f"C{i}", arguments={}),
                invariants="x",
            )
            for i in range(1, 4)
        ]

        result = coord.execute_saga("saga-lifo", steps)
        assert result is False
        # Steps 1,2 were committed then compensated in reverse
        assert call_order == ["C2", "C1"]


# ─────────────────────────────────────────────────────────────────────
# Rollback on Execution Exception
# ─────────────────────────────────────────────────────────────────────

class TestRollbackOnExecutionException:
    """Validate compensations fire when the sandbox raises an exception."""

    def test_execute_saga_rollback_on_execution_exception(
        self, mock_verifier, mock_sandbox
    ):
        from src.orchestrator.coordinator import SagaTransactionCoordinator

        # First execute succeeds, second raises
        mock_sandbox.execute = MagicMock(
            side_effect=[{"status": "SUCCESS"}, RuntimeError("disk full")]
        )

        coord = SagaTransactionCoordinator(mock_verifier, mock_sandbox)
        coord.start_transaction_log("saga-exc", "exc test", "t1")

        steps = [
            SagaStep(
                step_id="s1", step_name="step-1",
                action=ActionPayload(tool_name="T1", arguments={}),
                compensation=ActionPayload(tool_name="C1", arguments={}),
                invariants="",
            ),
            SagaStep(
                step_id="s2", step_name="step-2",
                action=ActionPayload(tool_name="T2", arguments={}),
                compensation=ActionPayload(tool_name="C2", arguments={}),
                invariants="",
            ),
        ]

        result = coord.execute_saga("saga-exc", steps)
        assert result is False
        assert steps[1].status == "FAILED"
        assert "Runtime Execution Exception" in steps[1].error
        # Compensation for step-1 (the only committed step) should have fired
        mock_sandbox.execute_compensation.assert_called_once()

    def test_exception_step_records_error_message(
        self, mock_verifier, mock_sandbox
    ):
        from src.orchestrator.coordinator import SagaTransactionCoordinator

        mock_sandbox.execute = MagicMock(side_effect=ValueError("bad input"))

        coord = SagaTransactionCoordinator(mock_verifier, mock_sandbox)
        step = SagaStep(
            step_id="s1", step_name="step-1",
            action=ActionPayload(tool_name="T", arguments={}),
            compensation=ActionPayload(tool_name="C", arguments={}),
            invariants="",
        )

        result = coord.execute_saga("saga-err", [step])
        assert result is False
        assert "bad input" in step.error


# ─────────────────────────────────────────────────────────────────────
# Compensation Failure Handling
# ─────────────────────────────────────────────────────────────────────

class TestCompensationFailure:
    """Validate behaviour when sandbox.execute_compensation returns False."""

    def test_compensation_failure_handling(self, mock_verifier, mock_sandbox):
        from src.orchestrator.coordinator import SagaTransactionCoordinator

        # Step 1 executes ok, step 2 fails verification → triggers compensation
        verifier = MagicMock()
        verifier.verify = MagicMock(side_effect=[(True, "OK"), (False, "bad")])
        mock_sandbox.execute_compensation = MagicMock(return_value=False)

        coord = SagaTransactionCoordinator(verifier, mock_sandbox)
        coord.start_transaction_log("saga-cf", "comp fail", "t1")

        steps = [
            SagaStep(
                step_id="s1", step_name="step-1",
                action=ActionPayload(tool_name="T1", arguments={}),
                compensation=ActionPayload(tool_name="C1", arguments={}),
                invariants="x",
            ),
            SagaStep(
                step_id="s2", step_name="step-2",
                action=ActionPayload(tool_name="T2", arguments={}),
                compensation=ActionPayload(tool_name="C2", arguments={}),
                invariants="x",
            ),
        ]

        result = coord.execute_saga("saga-cf", steps)
        assert result is False
        # The committed step's compensation failed
        assert steps[0].status == "COMPENSATION_FAILED"

    def test_compensation_exception_marks_step(self, mock_verifier, mock_sandbox):
        """If compensation itself raises, the step is marked COMPENSATION_FAILED."""
        from src.orchestrator.coordinator import SagaTransactionCoordinator

        verifier = MagicMock()
        verifier.verify = MagicMock(side_effect=[(True, "OK"), (False, "bad")])
        mock_sandbox.execute_compensation = MagicMock(
            side_effect=RuntimeError("compensation boom")
        )

        coord = SagaTransactionCoordinator(verifier, mock_sandbox)
        coord.start_transaction_log("saga-ce", "comp exc", "t1")

        steps = [
            SagaStep(
                step_id="s1", step_name="step-1",
                action=ActionPayload(tool_name="T1", arguments={}),
                compensation=ActionPayload(tool_name="C1", arguments={}),
                invariants="x",
            ),
            SagaStep(
                step_id="s2", step_name="step-2",
                action=ActionPayload(tool_name="T2", arguments={}),
                compensation=ActionPayload(tool_name="C2", arguments={}),
                invariants="x",
            ),
        ]

        result = coord.execute_saga("saga-ce", steps)
        assert result is False
        assert steps[0].status == "COMPENSATION_FAILED"


# ─────────────────────────────────────────────────────────────────────
# Callback Invocation
# ─────────────────────────────────────────────────────────────────────

class TestCallbackInvocation:
    """Validate that the optional callback receives correct lifecycle events."""

    def test_callback_invoked_on_success(self, coordinator, sample_steps):
        cb = MagicMock()
        coordinator.start_transaction_log("saga-cb", "callback test", "t1")
        coordinator.execute_saga("saga-cb", sample_steps, callback=cb)

        # Callback should have been called with RUNNING and COMMITTED for each step
        assert cb.call_count == len(sample_steps) * 2  # RUNNING + COMMITTED per step
        # First call should be (step, "RUNNING", "")
        first_call_args = cb.call_args_list[0]
        assert first_call_args[0][1] == "RUNNING"
        assert first_call_args[0][2] == ""
        # Second call should be (step, "COMMITTED", "")
        second_call_args = cb.call_args_list[1]
        assert second_call_args[0][1] == "COMMITTED"

    def test_callback_invoked_on_failure(self, failing_verifier, mock_sandbox):
        from src.orchestrator.coordinator import SagaTransactionCoordinator

        cb = MagicMock()
        coord = SagaTransactionCoordinator(failing_verifier, mock_sandbox)
        coord.start_transaction_log("saga-cbf", "fail cb", "t1")

        step = SagaStep(
            step_id="s1", step_name="step-1",
            action=ActionPayload(tool_name="T", arguments={}),
            compensation=ActionPayload(tool_name="C", arguments={}),
            invariants="x",
        )

        coord.execute_saga("saga-cbf", [step], callback=cb)

        # Should receive RUNNING then FAILED
        statuses = [c[0][1] for c in cb.call_args_list]
        assert "RUNNING" in statuses
        assert "FAILED" in statuses

    def test_callback_receives_step_object(self, coordinator):
        """The first argument to the callback is always the SagaStep itself."""
        cb = MagicMock()
        step = SagaStep(
            step_id="s1", step_name="tracked",
            action=ActionPayload(tool_name="T", arguments={}),
            compensation=ActionPayload(tool_name="C", arguments={}),
            invariants="",
        )

        coordinator.execute_saga("saga-track", [step], callback=cb)

        for c in cb.call_args_list:
            assert c[0][0] is step
