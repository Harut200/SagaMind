"""
SagaMind Durable Saga State Store
=================================

Persists saga lifecycle state and the compensation log so in-flight transactions survive a
process restart and can be rolled back on recovery — closing the previous gap where saga
state lived only in an in-process dict (``coordinator.active_sagas``) and ``db_client`` was
always ``None``.

Backends, selected automatically in order of preference:

1. **PostgreSQL / TimescaleDB** (``saga_transactions`` + ``saga_compensations`` tables) — the
   durable system of record.
2. **Redis** — fast shared state when Postgres is unavailable (this is where Redis, previously
   declared but unused, is now wired in).
3. **In-memory** — development / test fallback (no durability, but a working interface).

The store implements the interface the coordinator already calls
(``write_transaction_state``) plus ``append_compensation`` and ``list_incomplete`` used for
crash recovery.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.config import settings
from src.models import SagaStatus

logger = logging.getLogger("SagaMind.Orchestrator.StateStore")

_TERMINAL = {
    SagaStatus.COMMITTED.value,
    SagaStatus.ROLLED_BACK.value,
    SagaStatus.COMPENSATION_FAILED.value,
    SagaStatus.FAILED.value,
}


class SagaStateStore:
    """Durable saga state + compensation log with graceful backend degradation."""

    def __init__(self) -> None:
        self.backend = "memory"
        self._pg: Any = None
        self._redis: Any = None
        # In-memory mirrors (also the fallback store).
        self._state: dict[str, dict[str, Any]] = {}
        self._comps: dict[str, list[dict[str, Any]]] = {}

        forced = settings.state_store_backend.lower()
        if forced == "memory":
            pass  # explicit in-memory (hermetic tests / single-process dev)
        elif forced == "postgres":
            if not self._try_postgres():
                raise RuntimeError("STATE_STORE_BACKEND=postgres but Postgres is unavailable.")
            self.backend = "postgres"
        elif forced == "redis":
            if not self._try_redis():
                raise RuntimeError("STATE_STORE_BACKEND=redis but Redis is unavailable.")
            self.backend = "redis"
        elif self._try_postgres():  # auto-detect, most-durable first
            self.backend = "postgres"
        elif self._try_redis():
            self.backend = "redis"
        elif settings.require_backends:
            raise RuntimeError("REQUIRE_BACKENDS is set but no saga state backend is available.")
        logger.info("Saga state store backend: %s", self.backend)

    # ── Backend probes ──────────────────────────────────────────────────
    def _try_postgres(self) -> bool:
        try:
            import psycopg2

            self._pg = psycopg2.connect(
                host=settings.db_host,
                port=settings.db_port,
                dbname=settings.db_name,
                user=settings.db_user,
                password=settings.db_pass,
            )
            self._pg.autocommit = True
            self._ensure_pg_schema()
            return True
        except Exception as exc:  # noqa: BLE001 - optional backend
            logger.debug("Postgres saga store unavailable: %s", exc)
            self._pg = None
            return False

    def _try_redis(self) -> bool:
        try:
            import redis

            self._redis = redis.Redis(host=settings.redis_host, port=settings.redis_port, decode_responses=True)
            self._redis.ping()
            return True
        except Exception as exc:  # noqa: BLE001 - optional backend
            logger.debug("Redis saga store unavailable: %s", exc)
            self._redis = None
            return False

    def _ensure_pg_schema(self) -> None:
        with self._pg.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS saga_transactions (
                    saga_id    UUID PRIMARY KEY,
                    tenant_id  VARCHAR(50),
                    goal       TEXT,
                    status     VARCHAR(32) NOT NULL,
                    metadata   JSONB,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS saga_compensations (
                    id        BIGSERIAL PRIMARY KEY,
                    saga_id   UUID NOT NULL,
                    seq       INT NOT NULL,
                    tool_name VARCHAR(64) NOT NULL,
                    arguments JSONB NOT NULL
                );
                """
            )

    # ── Writes ──────────────────────────────────────────────────────────
    def write_transaction_state(self, saga_id: str, status: str, metadata: dict[str, Any]) -> None:
        """Upsert the saga's status and merge metadata (coordinator-facing API)."""
        if self.backend == "postgres":
            self._pg_write_state(saga_id, status, metadata)
        elif self.backend == "redis":
            self._redis.hset(f"saga:{saga_id}", mapping={"status": status, "metadata": json.dumps(metadata)})
            if status in _TERMINAL:
                self._redis.srem("sagas:incomplete", saga_id)
            else:
                self._redis.sadd("sagas:incomplete", saga_id)
        rec = self._state.setdefault(saga_id, {"saga_id": saga_id, "metadata": {}})
        rec["status"] = status
        rec["metadata"].update(metadata or {})

    def append_compensation(self, saga_id: str, tool_name: str, arguments: dict[str, Any]) -> None:
        """Persist a committed step's compensation so it can be replayed on recovery."""
        entry = {"tool_name": tool_name, "arguments": arguments}
        self._comps.setdefault(saga_id, []).append(entry)
        seq = len(self._comps[saga_id])
        if self.backend == "postgres":
            with self._pg.cursor() as cur:
                cur.execute(
                    "INSERT INTO saga_compensations (saga_id, seq, tool_name, arguments) VALUES (%s, %s, %s, %s);",
                    (saga_id, seq, tool_name, json.dumps(arguments)),
                )
        elif self.backend == "redis":
            self._redis.rpush(f"saga:{saga_id}:comps", json.dumps(entry))

    def _pg_write_state(self, saga_id: str, status: str, metadata: dict[str, Any]) -> None:
        with self._pg.cursor() as cur:
            cur.execute(
                """
                INSERT INTO saga_transactions (saga_id, tenant_id, goal, status, metadata, updated_at)
                VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (saga_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    metadata = COALESCE(saga_transactions.metadata, '{}'::jsonb) || EXCLUDED.metadata,
                    tenant_id = COALESCE(EXCLUDED.tenant_id, saga_transactions.tenant_id),
                    goal = COALESCE(EXCLUDED.goal, saga_transactions.goal),
                    updated_at = now();
                """,
                (
                    saga_id,
                    metadata.get("tenant_id"),
                    metadata.get("goal"),
                    status,
                    json.dumps(metadata or {}),
                ),
            )

    # ── Reads / recovery ────────────────────────────────────────────────
    def list_incomplete(self) -> list[dict[str, Any]]:
        """Return non-terminal sagas with their ordered compensation log."""
        if self.backend == "postgres":
            return self._pg_list_incomplete()
        if self.backend == "redis":
            return self._redis_list_incomplete()
        return [
            {"saga_id": sid, "compensations": list(self._comps.get(sid, []))}
            for sid, rec in self._state.items()
            if rec.get("status") not in _TERMINAL
        ]

    def _pg_list_incomplete(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with self._pg.cursor() as cur:
            cur.execute(
                "SELECT saga_id FROM saga_transactions WHERE status NOT IN %s;",
                (tuple(_TERMINAL),),
            )
            saga_ids = [str(r[0]) for r in cur.fetchall()]
            for sid in saga_ids:
                cur.execute(
                    "SELECT tool_name, arguments FROM saga_compensations WHERE saga_id = %s ORDER BY seq;",
                    (sid,),
                )
                comps = [{"tool_name": r[0], "arguments": r[1]} for r in cur.fetchall()]
                out.append({"saga_id": sid, "compensations": comps})
        return out

    def _redis_list_incomplete(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for sid in self._redis.smembers("sagas:incomplete"):
            comps = [json.loads(c) for c in self._redis.lrange(f"saga:{sid}:comps", 0, -1)]
            out.append({"saga_id": sid, "compensations": comps})
        return out

    def close(self) -> None:
        if self._pg is not None:
            try:
                self._pg.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Error closing Postgres saga store: %s", exc)
