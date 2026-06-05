"""
SagaMind Durable Saga State Store
=================================

Persists saga lifecycle state and the compensation log so in-flight transactions survive a
process restart and can be rolled back on recovery.

Backends, selected automatically in order of preference:

1. **PostgreSQL / TimescaleDB** — ``saga_transactions`` + ``saga_compensations`` +
   ``saga_dead_letters`` + ``saga_step_idempotency`` tables.  Uses a
   ``ThreadedConnectionPool`` (min 2, max 10) so concurrent API workers do not race over a
   single connection.  Reconnects automatically when the pooled connection is stale.
2. **Redis** — fast shared state when Postgres is unavailable.
3. **In-memory** — development / test fallback (no durability).

Interfaces consumed by the coordinator:

* ``write_transaction_state(saga_id, status, metadata)``
* ``append_compensation(saga_id, tool_name, arguments)``
* ``list_incomplete() -> list[dict]``
* ``step_already_committed(saga_id, idempotency_key) -> bool``
* ``mark_step_committed(saga_id, idempotency_key)``
* ``push_dead_letter(saga_id, step_name, error)``
* ``list_dead_letters() -> list[dict]``
* ``close()``
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
        self._pg_pool: Any = None
        self._redis: Any = None
        # In-memory store — only written when backend == "memory".
        self._state: dict[str, dict[str, Any]] = {}
        self._comps: dict[str, list[dict[str, Any]]] = {}
        self._idem: dict[str, set[str]] = {}      # saga_id → committed idempotency keys
        self._dead: list[dict[str, Any]] = []

        forced = settings.state_store_backend.lower()
        if forced == "memory":
            pass
        elif forced == "postgres":
            if not self._try_postgres():
                raise RuntimeError("STATE_STORE_BACKEND=postgres but Postgres is unavailable.")
            self.backend = "postgres"
        elif forced == "redis":
            if not self._try_redis():
                raise RuntimeError("STATE_STORE_BACKEND=redis but Redis is unavailable.")
            self.backend = "redis"
        elif self._try_postgres():
            self.backend = "postgres"
        elif self._try_redis():
            self.backend = "redis"
        elif settings.require_backends:
            raise RuntimeError("REQUIRE_BACKENDS is set but no saga state backend is available.")
        logger.info("Saga state store backend: %s", self.backend)

    # ── Backend probes ──────────────────────────────────────────────────
    def _try_postgres(self) -> bool:
        try:
            from psycopg2 import pool as pg_pool

            self._pg_pool = pg_pool.ThreadedConnectionPool(
                minconn=2,
                maxconn=10,
                host=settings.db_host,
                port=settings.db_port,
                dbname=settings.db_name,
                user=settings.db_user,
                password=settings.db_pass,
            )
            self._ensure_pg_schema()
            return True
        except Exception as exc:  # noqa: BLE001 - optional backend
            logger.debug("Postgres saga store unavailable: %s", exc)
            self._pg_pool = None
            return False

    def _try_redis(self) -> bool:
        try:
            import redis

            self._redis = redis.Redis(
                host=settings.redis_host, port=settings.redis_port, decode_responses=True
            )
            self._redis.ping()
            return True
        except Exception as exc:  # noqa: BLE001 - optional backend
            logger.debug("Redis saga store unavailable: %s", exc)
            self._redis = None
            return False

    def _pg_conn(self) -> Any:
        """Checkout a connection from the pool, reconnecting the pool if stale."""
        try:
            return self._pg_pool.getconn()
        except Exception:  # noqa: BLE001 - pool closed / all connections broken
            self._try_postgres()
            return self._pg_pool.getconn()

    def _pg_return(self, conn: Any) -> None:
        try:
            self._pg_pool.putconn(conn)
        except Exception:  # noqa: BLE001
            pass

    def _ensure_pg_schema(self) -> None:
        conn = self._pg_conn()
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
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
                    CREATE TABLE IF NOT EXISTS saga_compensations (
                        id        BIGSERIAL PRIMARY KEY,
                        saga_id   UUID NOT NULL,
                        seq       INT NOT NULL,
                        tool_name VARCHAR(64) NOT NULL,
                        arguments JSONB NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS saga_step_idempotency (
                        saga_id         UUID NOT NULL,
                        idempotency_key VARCHAR(128) NOT NULL,
                        committed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                        PRIMARY KEY (saga_id, idempotency_key)
                    );
                    CREATE TABLE IF NOT EXISTS saga_dead_letters (
                        id          BIGSERIAL PRIMARY KEY,
                        saga_id     UUID NOT NULL,
                        step_name   TEXT,
                        error       TEXT,
                        occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    );
                    """
                )
        finally:
            self._pg_return(conn)

    # ── Writes ──────────────────────────────────────────────────────────
    def write_transaction_state(self, saga_id: str, status: str, metadata: dict[str, Any]) -> None:
        """Upsert saga status and metadata."""
        if self.backend == "postgres":
            self._pg_write_state(saga_id, status, metadata)
        elif self.backend == "redis":
            self._redis.hset(
                f"saga:{saga_id}",
                mapping={"status": status, "metadata": json.dumps(metadata)},
            )
            if status in _TERMINAL:
                self._redis.srem("sagas:incomplete", saga_id)
            else:
                self._redis.sadd("sagas:incomplete", saga_id)
        else:
            rec = self._state.setdefault(saga_id, {"saga_id": saga_id, "metadata": {}})
            rec["status"] = status
            rec["metadata"].update(metadata or {})

    def append_compensation(self, saga_id: str, tool_name: str, arguments: dict[str, Any]) -> None:
        """Persist a committed step's compensation for crash-recovery replay."""
        if self.backend == "postgres":
            conn = self._pg_conn()
            try:
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COALESCE(MAX(seq), 0) + 1 FROM saga_compensations WHERE saga_id = %s;",
                        (saga_id,),
                    )
                    seq = cur.fetchone()[0]
                    cur.execute(
                        "INSERT INTO saga_compensations (saga_id, seq, tool_name, arguments) "
                        "VALUES (%s, %s, %s, %s);",
                        (saga_id, seq, tool_name, json.dumps(arguments)),
                    )
            finally:
                self._pg_return(conn)
        elif self.backend == "redis":
            self._redis.rpush(
                f"saga:{saga_id}:comps",
                json.dumps({"tool_name": tool_name, "arguments": arguments}),
            )
        else:
            self._comps.setdefault(saga_id, []).append(
                {"tool_name": tool_name, "arguments": arguments}
            )

    def _pg_write_state(self, saga_id: str, status: str, metadata: dict[str, Any]) -> None:
        conn = self._pg_conn()
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO saga_transactions
                        (saga_id, tenant_id, goal, status, metadata, updated_at)
                    VALUES (%s, %s, %s, %s, %s, now())
                    ON CONFLICT (saga_id) DO UPDATE SET
                        status     = EXCLUDED.status,
                        metadata   = COALESCE(saga_transactions.metadata, '{}'::jsonb)
                                     || EXCLUDED.metadata,
                        tenant_id  = COALESCE(EXCLUDED.tenant_id, saga_transactions.tenant_id),
                        goal       = COALESCE(EXCLUDED.goal, saga_transactions.goal),
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
        finally:
            self._pg_return(conn)

    # ── Idempotency ─────────────────────────────────────────────────────
    def step_already_committed(self, saga_id: str, idempotency_key: str) -> bool:
        """Return True if this (saga_id, idempotency_key) pair was previously committed."""
        if self.backend == "postgres":
            conn = self._pg_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM saga_step_idempotency WHERE saga_id = %s AND idempotency_key = %s;",
                        (saga_id, idempotency_key),
                    )
                    return cur.fetchone() is not None
            finally:
                self._pg_return(conn)
        elif self.backend == "redis":
            return bool(self._redis.sismember(f"saga:{saga_id}:idem", idempotency_key))
        else:
            return idempotency_key in self._idem.get(saga_id, set())

    def mark_step_committed(self, saga_id: str, idempotency_key: str) -> None:
        """Record that this step was successfully committed (for deduplication)."""
        if self.backend == "postgres":
            conn = self._pg_conn()
            try:
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO saga_step_idempotency (saga_id, idempotency_key) "
                        "VALUES (%s, %s) ON CONFLICT DO NOTHING;",
                        (saga_id, idempotency_key),
                    )
            finally:
                self._pg_return(conn)
        elif self.backend == "redis":
            self._redis.sadd(f"saga:{saga_id}:idem", idempotency_key)
        else:
            self._idem.setdefault(saga_id, set()).add(idempotency_key)

    # ── Dead-letter queue ────────────────────────────────────────────────
    def push_dead_letter(self, saga_id: str, step_name: str, error: str) -> None:
        """Record a saga that reached COMPENSATION_FAILED for manual operator review."""
        entry: dict[str, Any] = {
            "saga_id": saga_id,
            "step_name": step_name,
            "error": error,
        }
        if self.backend == "postgres":
            conn = self._pg_conn()
            try:
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO saga_dead_letters (saga_id, step_name, error) VALUES (%s, %s, %s);",
                        (saga_id, step_name, error),
                    )
            finally:
                self._pg_return(conn)
        elif self.backend == "redis":
            self._redis.lpush("sagas:dead_letter", json.dumps(entry))
        else:
            self._dead.append(entry)

    def list_dead_letters(self) -> list[dict[str, Any]]:
        """Return all dead-letter entries ordered newest-first."""
        if self.backend == "postgres":
            conn = self._pg_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT saga_id, step_name, error, occurred_at "
                        "FROM saga_dead_letters ORDER BY occurred_at DESC LIMIT 500;"
                    )
                    return [
                        {
                            "saga_id": str(r[0]),
                            "step_name": r[1],
                            "error": r[2],
                            "occurred_at": r[3].isoformat() if r[3] else None,
                        }
                        for r in cur.fetchall()
                    ]
            finally:
                self._pg_return(conn)
        elif self.backend == "redis":
            raw = self._redis.lrange("sagas:dead_letter", 0, 499)
            return [json.loads(r) for r in raw]
        else:
            return list(reversed(self._dead))

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
        conn = self._pg_conn()
        try:
            out: list[dict[str, Any]] = []
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT saga_id FROM saga_transactions WHERE status NOT IN %s;",
                    (tuple(_TERMINAL),),
                )
                saga_ids = [str(r[0]) for r in cur.fetchall()]
                for sid in saga_ids:
                    cur.execute(
                        "SELECT tool_name, arguments FROM saga_compensations "
                        "WHERE saga_id = %s ORDER BY seq;",
                        (sid,),
                    )
                    comps = [{"tool_name": r[0], "arguments": r[1]} for r in cur.fetchall()]
                    out.append({"saga_id": sid, "compensations": comps})
            return out
        finally:
            self._pg_return(conn)

    def _redis_list_incomplete(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for sid in self._redis.smembers("sagas:incomplete"):
            comps = [
                json.loads(c) for c in self._redis.lrange(f"saga:{sid}:comps", 0, -1)
            ]
            out.append({"saga_id": sid, "compensations": comps})
        return out

    def close(self) -> None:
        if self._pg_pool is not None:
            try:
                self._pg_pool.closeall()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Error closing Postgres pool: %s", exc)
