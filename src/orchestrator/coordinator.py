"""
SagaMind Saga Transaction Coordinator
======================================

Stateful saga transaction engine enforcing eventual consistency across
non-deterministic multi-agent execution paths.

Architecture:
    - Forward execution: steps are committed sequentially through verification gates.
    - Rollback: compensations are executed in LIFO (reverse chronological) order.
    - Each step passes through a Z3 verification gate before sandbox execution.
    - Idempotency: duplicate step submissions (same idempotency_key) are detected and
      short-circuited before re-execution.
    - Dead-letter: COMPENSATION_FAILED sagas are pushed to the dead-letter queue for
      operator review instead of being silently abandoned.
"""

import logging
import time
from collections.abc import Callable
from typing import Any

from src.models import ActionPayload, SagaStatus, SagaTransaction, StepStatus
from src.observability import metrics

logger = logging.getLogger("SagaMind.Orchestrator")


class CoordinatorError(Exception):
    """Coordinator-level transaction failures."""


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
        self.active_sagas: dict[str, SagaTransaction] = {}

    # ── Status ───────────────────────────────────────────────────────────
    def get_saga_status(self, saga_id: str) -> dict[str, Any] | None:
        """Return a snapshot of the saga's current state, or None if unknown."""
        saga = self.active_sagas.get(saga_id)
        if saga is None:
            return None
        return {
            "saga_id": saga.saga_id,
            "tenant_id": saga.tenant_id,
            "goal": saga.goal,
            "status": saga.status,
            "start_time": saga.start_time,
            "completed_steps": [s.step_name for s in saga.completed_steps],
        }

    # ── Recovery ─────────────────────────────────────────────────────────
    def recover(self) -> int:
        """Replay persisted compensations for sagas left incomplete by a crash.

        Returns the number of sagas rolled back.
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
                    self.sandbox.execute_compensation(ActionPayload(comp["tool_name"], comp.get("arguments", {})))
                except Exception as exc:  # noqa: BLE001 - best-effort recovery
                    logger.error("[RECOVERY] Compensation failed for saga %s: %s", saga_id, exc)
            self.db.write_transaction_state(saga_id, SagaStatus.ROLLED_BACK.value, {"recovered": True})
            recovered += 1
        if recovered:
            logger.warning("[RECOVERY] Rolled back %d incomplete saga(s) on startup.", recovered)
        return recovered

    # ── Eviction ─────────────────────────────────────────────────────────
    def _evict_if_needed(self) -> None:
        """Bound in-memory saga retention to prevent unbounded growth."""
        if len(self.active_sagas) <= self.max_active_sagas:
            return
        terminal = {
            SagaStatus.COMMITTED.value,
            SagaStatus.ROLLED_BACK.value,
            SagaStatus.COMPENSATION_FAILED.value,
        }
        for sid in list(self.active_sagas.keys()):
            if len(self.active_sagas) <= self.max_active_sagas:
                break
            if self.active_sagas[sid].status in terminal:
                del self.active_sagas[sid]

    # ── Lifecycle ────────────────────────────────────────────────────────
    def start_transaction_log(self, saga_id: str, goal: str, tenant_id: str) -> None:
        """Initialize a new saga transaction session."""
        self.active_sagas[saga_id] = SagaTransaction(
            saga_id=saga_id,
            tenant_id=tenant_id,
            goal=goal,
            status=SagaStatus.RUNNING.value,
            start_time=time.time(),
        )
        metrics.inc("sagas_started")
        logger.info(
            "[SAGA-%s] Transaction initialized for tenant '%s'. Goal: '%s'",
            saga_id,
            tenant_id,
            goal,
        )
        if self.db:
            self.db.write_transaction_state(saga_id, SagaStatus.RUNNING.value, {"goal": goal, "tenant_id": tenant_id})

    # ── Execution ────────────────────────────────────────────────────────
    def execute_saga(
        self,
        saga_id: str,
        steps: list[Any],
        callback: Callable[[Any, str, str], None] | None = None,
    ) -> bool:
        """Execute a sequence of SagaStep tasks.

        Raises ``CoordinatorError`` if ``saga_id`` is unknown (caller must call
        ``start_transaction_log`` first or use the ``/saga/start`` endpoint).

        Returns True on full commit, False when a rollback occurred.
        """
        if saga_id not in self.active_sagas:
            raise CoordinatorError(f"Saga '{saga_id}' not found. Call start_transaction_log first.")

        saga = self.active_sagas[saga_id]
        completed = saga.completed_steps

        for step in steps:
            # ── Idempotency check ────────────────────────────────────────
            if (
                step.idempotency_key
                and self.db
                and hasattr(self.db, "step_already_committed")
                and self.db.step_already_committed(saga_id, step.idempotency_key)
            ):
                logger.info(
                    "[SAGA-%s] Step '%s' already committed (idempotency_key=%s). Skipping.",
                    saga_id,
                    step.step_name,
                    step.idempotency_key,
                )
                step.status = StepStatus.COMMITTED.value
                completed.append(step)
                continue

            step.status = StepStatus.RUNNING.value
            logger.info("[SAGA-%s] Initiating Step: '%s'", saga_id, step.step_name)
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
                    logger.error(
                        "[SAGA-%s] Invariant violation at step '%s': %s",
                        saga_id,
                        step.step_name,
                        explanation,
                    )
                    if callback:
                        callback(step, StepStatus.FAILED.value, step.error)
                    self.execute_compensations(saga_id, completed, callback)
                    return False

                # 2. Execute in sandbox
                with metrics.time("step_seconds"):
                    self.sandbox.execute(step.action)
                step.status = StepStatus.COMMITTED.value
                completed.append(step)

                # Persist compensation + idempotency record
                if self.db and hasattr(self.db, "append_compensation"):
                    self.db.append_compensation(saga_id, step.compensation.tool_name, step.compensation.arguments)
                if step.idempotency_key and self.db and hasattr(self.db, "mark_step_committed"):
                    self.db.mark_step_committed(saga_id, step.idempotency_key)

                logger.info("[SAGA-%s] Step '%s' executed and committed successfully.", saga_id, step.step_name)
                if callback:
                    callback(step, StepStatus.COMMITTED.value, "")

            except Exception as e:
                step.status = StepStatus.FAILED.value
                step.error = f"Runtime Execution Exception: {e!s}"
                logger.error(
                    "[SAGA-%s] Exception at step '%s': %s",
                    saga_id,
                    step.step_name,
                    step.error,
                    exc_info=True,
                )
                if callback:
                    callback(step, StepStatus.FAILED.value, step.error)
                self.execute_compensations(saga_id, completed, callback)
                return False

        saga.status = SagaStatus.COMMITTED.value
        metrics.inc("sagas_committed")
        if self.db:
            self.db.write_transaction_state(saga_id, SagaStatus.COMMITTED.value, {})
        logger.info("[SAGA-%s] Saga transaction committed successfully.", saga_id)
        self._evict_if_needed()
        return True

    # ── Compensations ────────────────────────────────────────────────────
    def execute_compensations(
        self,
        saga_id: str,
        completed_steps: list[Any],
        callback: Callable[[Any, str, str], None] | None = None,
    ) -> None:
        """Execute compensations in LIFO order. Guarantees eventual consistency."""
        logger.warning("[SAGA-%s] Initiating rollback for %d completed step(s).", saga_id, len(completed_steps))
        if self.db:
            self.db.write_transaction_state(saga_id, SagaStatus.COMPENSATING.value, {})

        for step in reversed(completed_steps):
            step.status = StepStatus.COMPENSATING.value
            logger.info("[SAGA-%s] Reverting step: '%s'", saga_id, step.step_name)
            if callback:
                callback(step, StepStatus.COMPENSATING.value, "")

            try:
                comp_ok = self.sandbox.execute_compensation(step.compensation)
                if comp_ok:
                    step.status = StepStatus.ROLLED_BACK.value
                    logger.info("[SAGA-%s] Rollback complete for step: '%s'", saga_id, step.step_name)
                    if callback:
                        callback(step, StepStatus.ROLLED_BACK.value, "")
                else:
                    self._handle_compensation_failure(saga_id, step, "Compensation returned False", callback)
                    return
            except Exception as e:
                self._handle_compensation_failure(saga_id, step, str(e), callback)
                return

        if saga_id in self.active_sagas:
            self.active_sagas[saga_id].status = SagaStatus.ROLLED_BACK.value
        metrics.inc("sagas_rolled_back")
        if self.db:
            self.db.write_transaction_state(saga_id, SagaStatus.ROLLED_BACK.value, {})
        logger.warning("[SAGA-%s] Rollback complete. Eventual consistency restored.", saga_id)

    def _handle_compensation_failure(
        self,
        saga_id: str,
        step: Any,
        error: str,
        callback: Callable[[Any, str, str], None] | None,
    ) -> None:
        step.status = StepStatus.COMPENSATION_FAILED.value
        metrics.inc("compensations_failed")
        logger.error(
            "[SAGA-%s] [CRITICAL] Compensation failed for step '%s': %s — system may be inconsistent.",
            saga_id,
            step.step_name,
            error,
        )
        if callback:
            callback(step, StepStatus.COMPENSATION_FAILED.value, error)
        if saga_id in self.active_sagas:
            self.active_sagas[saga_id].status = SagaStatus.COMPENSATION_FAILED.value
        if self.db:
            self.db.write_transaction_state(
                saga_id,
                SagaStatus.COMPENSATION_FAILED.value,
                {"failed_step": step.step_name},
            )
            if hasattr(self.db, "push_dead_letter"):
                self.db.push_dead_letter(saga_id, step.step_name, error)
