"""
SagaMind Saga Transaction Coordinator
======================================

Stateful saga transaction engine enforcing eventual consistency across
non-deterministic multi-agent execution paths.

Architecture:
    - Forward execution: steps are committed sequentially through verification gates.
    - Rollback: compensations are executed in LIFO (reverse chronological) order.
    - Each step passes through a Z3 verification gate before sandbox execution.
"""

import logging
import time
from collections.abc import Callable
from typing import Any

from src.models import ActionPayload, SagaStatus, StepStatus
from src.observability import metrics

# Setup logging channel
logger = logging.getLogger("SagaMind.Orchestrator")


class CoordinatorError(Exception):
    """Custom exception class for Coordinator-level transaction failures."""

    pass


class SagaTransactionCoordinator:
    """
    Stateful Saga transaction engine enforcing consistency across
    non-deterministic multi-agent paths.
    """

    def __init__(
        self,
        verifier_instance: Any,
        sandbox_instance: Any,
        db_client: Any | None = None,
        max_active_sagas: int = 10_000,
    ):
        self.verifier = verifier_instance
        self.sandbox = sandbox_instance
        self.db = db_client
        self.max_active_sagas = max_active_sagas
        self.active_sagas: dict[str, dict[str, Any]] = {}

    def get_saga_status(self, saga_id: str) -> dict[str, Any] | None:
        """Return a snapshot of the saga's current state, or None if unknown."""
        saga = self.active_sagas.get(saga_id)
        if saga is None:
            return None
        return {
            "saga_id": saga["saga_id"],
            "tenant_id": saga["tenant_id"],
            "goal": saga["goal"],
            "status": saga["status"],
            "start_time": saga["start_time"],
            "completed_steps": [s.step_name for s in saga["completed_steps"]],
        }

    def recover(self) -> int:
        """Replay persisted compensations for sagas left incomplete by a crash.

        Returns the number of sagas rolled back. Requires a durable ``db_client`` exposing
        ``list_incomplete`` / ``write_transaction_state`` (see ``SagaStateStore``).
        """
        if not self.db or not hasattr(self.db, "list_incomplete"):
            return 0
        recovered = 0
        for saga in self.db.list_incomplete():
            saga_id = saga["saga_id"]
            comps = saga.get("compensations", [])
            logger.warning("[RECOVERY] Compensating %d step(s) for incomplete saga %s", len(comps), saga_id)
            for comp in reversed(comps):
                try:
                    self.sandbox.execute_compensation(
                        ActionPayload(comp["tool_name"], comp.get("arguments", {}))
                    )
                except Exception as exc:  # noqa: BLE001 - best-effort recovery, continue
                    logger.error("[RECOVERY] Compensation failed for saga %s: %s", saga_id, exc)
            self.db.write_transaction_state(saga_id, SagaStatus.ROLLED_BACK.value, {"recovered": True})
            recovered += 1
        if recovered:
            logger.warning("[RECOVERY] Rolled back %d incomplete saga(s) on startup.", recovered)
        return recovered

    def _evict_if_needed(self) -> None:
        """Bound in-memory saga retention to avoid unbounded growth in long-running processes."""
        if len(self.active_sagas) <= self.max_active_sagas:
            return
        terminal = {
            SagaStatus.COMMITTED.value,
            SagaStatus.ROLLED_BACK.value,
            SagaStatus.COMPENSATION_FAILED.value,
        }
        # Oldest-first eviction of finished sagas only.
        for sid in list(self.active_sagas.keys()):
            if len(self.active_sagas) <= self.max_active_sagas:
                break
            if self.active_sagas[sid]["status"] in terminal:
                del self.active_sagas[sid]

    def start_transaction_log(self, saga_id: str, goal: str, tenant_id: str):
        """Initialize a new saga transaction session."""
        self.active_sagas[saga_id] = {
            "saga_id": saga_id,
            "tenant_id": tenant_id,
            "goal": goal,
            "status": SagaStatus.RUNNING.value,
            "start_time": time.time(),
            "completed_steps": [],
        }
        metrics.inc("sagas_started")
        logger.info(f"[SAGA-{saga_id}] Transaction initialized for tenant '{tenant_id}'. Goal: '{goal}'")
        if self.db:
            self.db.write_transaction_state(
                saga_id, SagaStatus.RUNNING.value, {"goal": goal, "tenant_id": tenant_id}
            )

    def execute_saga(
        self, saga_id: str, steps: list[Any], callback: Callable[[Any, str, str], None] | None = None
    ) -> bool:
        """
        Executes a sequence of SagaStep tasks.

        Args:
            saga_id:  Unique saga transaction identifier.
            steps:    Ordered list of SagaStep instances.
            callback: Optional status change callback ``fn(step, status, error)``.

        Returns:
            True if the entire chain succeeded, False if a rollback occurred.
        """
        if saga_id not in self.active_sagas:
            self.start_transaction_log(saga_id, "Workflow Execution", "default_tenant")

        saga_meta = self.active_sagas[saga_id]
        completed = saga_meta["completed_steps"]

        for step in steps:
            step.status = StepStatus.RUNNING.value
            logger.info(f"[SAGA-{saga_id}] Initiating Step: '{step.step_name}'")
            if callback:
                callback(step, StepStatus.RUNNING.value, "")

            try:
                # 1. Verification Gate
                with metrics.time("verify_seconds"):
                    ver_ok, explanation = self.verifier.verify(step.action.arguments, step.invariants)
                if not ver_ok:
                    metrics.inc("steps_rejected")
                    step.status = StepStatus.FAILED.value
                    step.error = f"Logic Solver check failed: {explanation}"
                    logger.error(f"[SAGA-{saga_id}] Invariant violation at step '{step.step_name}': {explanation}")
                    if callback:
                        callback(step, StepStatus.FAILED.value, step.error)
                    self.execute_compensations(saga_id, completed, callback)
                    return False

                # 2. Execute in sandbox
                with metrics.time("step_seconds"):
                    self.sandbox.execute(step.action)
                step.status = StepStatus.COMMITTED.value
                completed.append(step)
                # Persist the compensation so a crash mid-saga can be rolled back on recovery.
                if self.db and hasattr(self.db, "append_compensation"):
                    self.db.append_compensation(
                        saga_id, step.compensation.tool_name, step.compensation.arguments
                    )
                logger.info(f"[SAGA-{saga_id}] Step '{step.step_name}' executed and committed successfully.")
                if callback:
                    callback(step, StepStatus.COMMITTED.value, "")

            except Exception as e:
                step.status = StepStatus.FAILED.value
                step.error = f"Runtime Execution Exception: {str(e)}"
                logger.error(f"[SAGA-{saga_id}] Exception at step '{step.step_name}': {step.error}", exc_info=True)
                if callback:
                    callback(step, StepStatus.FAILED.value, step.error)
                self.execute_compensations(saga_id, completed, callback)
                return False

        saga_meta["status"] = SagaStatus.COMMITTED.value
        metrics.inc("sagas_committed")
        if self.db:
            self.db.write_transaction_state(saga_id, SagaStatus.COMMITTED.value, {})
        logger.info(f"[SAGA-{saga_id}] Saga transaction committed successfully.")
        self._evict_if_needed()
        return True

    def execute_compensations(
        self, saga_id: str, completed_steps: list[Any], callback: Callable[[Any, str, str], None] | None = None
    ):
        """
        Executes compensations in reverse chronological order (LIFO).
        Guarantees eventual consistency by undoing committed mutations.
        """
        logger.warning(f"[SAGA-{saga_id}] Initiating rollback for {len(completed_steps)} completed steps.")
        if self.db:
            self.db.write_transaction_state(saga_id, SagaStatus.COMPENSATING.value, {})

        for step in reversed(completed_steps):
            step.status = StepStatus.COMPENSATING.value
            logger.info(f"[SAGA-{saga_id}] Reverting step: '{step.step_name}'")
            if callback:
                callback(step, StepStatus.COMPENSATING.value, "")

            try:
                comp_ok = self.sandbox.execute_compensation(step.compensation)
                if comp_ok:
                    step.status = StepStatus.ROLLED_BACK.value
                    logger.info(f"[SAGA-{saga_id}] Rollback complete for step: '{step.step_name}'")
                    if callback:
                        callback(step, StepStatus.ROLLED_BACK.value, "")
                else:
                    step.status = StepStatus.COMPENSATION_FAILED.value
                    metrics.inc("compensations_failed")
                    logger.error(f"[SAGA-{saga_id}] [CRITICAL] Compensation failed for step '{step.step_name}'!")
                    if callback:
                        callback(step, StepStatus.COMPENSATION_FAILED.value, "Compensation execution failed.")
                    if self.db:
                        self.db.write_transaction_state(
                            saga_id, SagaStatus.COMPENSATION_FAILED.value, {"failed_step": step.step_name}
                        )
                    return
            except Exception as e:
                step.status = StepStatus.COMPENSATION_FAILED.value
                metrics.inc("compensations_failed")
                logger.error(
                    f"[SAGA-{saga_id}] [CRITICAL] Exception during rollback for step '{step.step_name}': {str(e)}"
                )
                if callback:
                    callback(step, StepStatus.COMPENSATION_FAILED.value, str(e))
                return

        self.active_sagas[saga_id]["status"] = SagaStatus.ROLLED_BACK.value
        metrics.inc("sagas_rolled_back")
        if self.db:
            self.db.write_transaction_state(saga_id, SagaStatus.ROLLED_BACK.value, {})
        logger.warning(f"[SAGA-{saga_id}] Rollback complete. Eventual consistency restored.")
