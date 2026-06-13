"""
SagaMind Distributed Saga Orchestration (Temporal) — §6.3
==========================================================

Optional Temporal-backed execution of saga steps, enabling durable, resumable saga
workflows across process restarts and multiple workers — distributed execution beyond
the single-process coordinator in ``coordinator.py``.

Disabled by default (``settings.temporal_target`` empty). When the ``temporalio``
package is absent or no Temporal server is configured, ``run_worker`` raises an
actionable error rather than silently no-opping, mirroring the gRPC codegen-required
pattern in ``grpc_server.py``. The single-process coordinator remains fully functional
and is the default execution path; this module is additive.

Usage (requires a running Temporal server and ``pip install temporalio``):

    python -m src.orchestrator.temporal_worker
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import timedelta
from typing import Any

from src.config import settings
from src.models import ActionPayload, SagaStep

logger = logging.getLogger("SagaMind.Orchestrator.Temporal")


def _build_step(raw: dict[str, Any]) -> SagaStep:
    return SagaStep(
        step_id=raw["step_id"],
        step_name=raw["step_name"],
        action=ActionPayload(raw["action"]["tool_name"], raw["action"]["arguments"]),
        compensation=ActionPayload(raw["compensation"]["tool_name"], raw["compensation"]["arguments"]),
        invariants=raw["invariants"],
        idempotency_key=raw.get("idempotency_key"),
        requires_approval=raw.get("requires_approval", False),
    )


def _serialize_step(step: SagaStep) -> dict[str, Any]:
    d = asdict(step)
    return d


try:
    from temporalio import activity, workflow

    @activity.defn
    async def execute_saga_activity(payload: dict[str, Any]) -> dict[str, Any]:
        """Activity wrapper around ``SagaTransactionCoordinator.execute_saga``.

        Constructs a fresh coordinator per activity invocation so the activity worker
        has no shared mutable state between sagas (each call is independently retryable).
        """
        from src.memory.consolidation import MemoryConsolidator  # noqa: F401 - import-time wiring parity
        from src.orchestrator.coordinator import SagaTransactionCoordinator
        from src.orchestrator.sandbox import WasmSandbox
        from src.orchestrator.state_store import SagaStateStore
        from src.verifier.z3_prover import Z3Verifier

        saga_store = SagaStateStore()
        coordinator = SagaTransactionCoordinator(Z3Verifier(), WasmSandbox(), db_client=saga_store)
        coordinator.start_transaction_log(payload["saga_id"], payload["goal"], payload["tenant_id"])
        steps = [_build_step(s) for s in payload["steps"]]
        success = coordinator.execute_saga(payload["saga_id"], steps)
        saga = coordinator.active_sagas[payload["saga_id"]]
        return {
            "success": success,
            "status": saga.status,
            "steps": [_serialize_step(s) for s in steps],
        }

    @workflow.defn
    class SagaWorkflow:
        """Durable workflow wrapping a single saga's execution as a Temporal activity.

        Temporal's own durable execution log (history) provides the distributed
        equivalent of ``SagaStateStore`` + ``coordinator.recover()`` — if the worker
        process crashes mid-activity, Temporal retries/replays from history.
        """

        @workflow.run
        async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
            return await workflow.execute_activity(
                execute_saga_activity,
                payload,
                start_to_close_timeout=timedelta(minutes=10),
            )

    _TEMPORAL_AVAILABLE = True
except ImportError:
    _TEMPORAL_AVAILABLE = False


async def run_worker() -> None:
    """Start a Temporal worker hosting ``SagaWorkflow``. Requires ``temporalio`` + a Temporal server."""
    if not _TEMPORAL_AVAILABLE:
        raise RuntimeError(
            "temporalio is not installed. Install the 'temporal' extra "
            "(`pip install sagamind[temporal]`) to enable distributed saga orchestration."
        )
    if not settings.temporal_target:
        raise RuntimeError("TEMPORAL_TARGET is not configured. Set it to your Temporal server address (host:port).")

    from temporalio.client import Client
    from temporalio.worker import Worker

    client = await Client.connect(settings.temporal_target, namespace=settings.temporal_namespace)
    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[SagaWorkflow],
        activities=[execute_saga_activity],
    )
    logger.info(
        "Temporal worker starting (target=%s, namespace=%s, task_queue=%s).",
        settings.temporal_target,
        settings.temporal_namespace,
        settings.temporal_task_queue,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(run_worker())
