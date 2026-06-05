"""
SagaMind Episodic Memory Store (TimescaleDB + pgvector)
=======================================================

Relational + vector store for episodic memory traces. Uses a connection pool and
pgvector cosine search when TimescaleDB is reachable, and a deterministic in-memory
emulator otherwise so that local development and the test-suite need no database.

Notes
-----
* The previous implementation referenced ``psycopg2.pool`` without importing the
  submodule, so the pool constructor always raised and was silently swallowed — the
  store therefore *never* used the database. Both the import and the exception handling
  are fixed here.
* Embeddings are bound through the pgvector adapter when available, otherwise as an
  explicit ``'[...]'`` vector literal, so inserts and ``<=>`` cosine queries work.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from typing import Any

from src.config import settings

logger = logging.getLogger("SagaMind.Memory.Timescale")


def _vector_literal(embedding: list[float]) -> str:
    """Render a Python vector as a pgvector text literal: ``[1,2,3]``."""
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


class TimescaleMemoryStore:
    """Episodic memory manager interfacing with TimescaleDB + pgvector."""

    def __init__(self) -> None:
        self.pool: Any | None = None
        self.pool_active = False
        self._vector_registered = False
        self.fallback_storage: list[dict[str, Any]] = []

        try:
            import psycopg2
            from psycopg2 import pool as pg_pool

            self.pool = pg_pool.SimpleConnectionPool(
                1,
                10,
                host=settings.db_host,
                port=settings.db_port,
                dbname=settings.db_name,
                user=settings.db_user,
                password=settings.db_pass,
            )
            self._register_vector_adapter()
            self.pool_active = True
            self.initialize_schema()
            logger.info("TimescaleDB connection pool successfully initialized.")
        except ImportError as exc:
            self._handle_unavailable("psycopg2 driver not installed", exc)
        except Exception as exc:  # noqa: BLE001 - DB drivers raise many connection errors
            self._handle_unavailable("TimescaleDB connection failed", exc)

    # ── Availability handling ───────────────────────────────────────────
    def _handle_unavailable(self, reason: str, exc: Exception) -> None:
        if settings.require_backends:
            raise RuntimeError(
                f"REQUIRE_BACKENDS is set but TimescaleDB is unavailable: {reason}: {exc}"
            ) from exc
        logger.warning("%s. Using in-memory episodic store. (%s)", reason, exc)

    def _register_vector_adapter(self) -> None:
        """Register the pgvector type adapter so list embeddings bind correctly."""
        try:
            import psycopg2

            from pgvector.psycopg2 import register_vector

            conn = self.pool.getconn()
            try:
                register_vector(conn)
                self._vector_registered = True
            finally:
                self.pool.putconn(conn)
        except Exception as exc:  # noqa: BLE001 - adapter is optional, fall back to literals
            logger.debug("pgvector adapter unavailable, using text literals: %s", exc)

    def _bind_embedding(self, embedding: list[float]) -> Any:
        return embedding if self._vector_registered else _vector_literal(embedding)

    # ── Schema ──────────────────────────────────────────────────────────
    def initialize_schema(self) -> None:
        """Create the extension, table, hypertable and vector index if absent."""
        conn = self.pool.getconn()
        try:
            with conn.cursor() as cursor:
                cursor.execute("CREATE EXTENSION IF NOT EXISTS vector;")
                cursor.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS episodic_memories (
                        memory_id UUID PRIMARY KEY,
                        tenant_id VARCHAR(50) NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL,
                        last_retrieved_at TIMESTAMPTZ NOT NULL,
                        agent_role VARCHAR(50) NOT NULL,
                        summary TEXT NOT NULL,
                        context_data JSONB,
                        importance_score DOUBLE PRECISION NOT NULL,
                        retrieval_count INT NOT NULL DEFAULT 0,
                        embedding VECTOR({settings.embedding_dim})
                    );
                    """
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_episodic_tenant "
                    "ON episodic_memories (tenant_id);"
                )
                # Approximate-nearest-neighbour index for cosine similarity search.
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_episodic_embedding "
                    "ON episodic_memories USING hnsw (embedding vector_cosine_ops);"
                )
                conn.commit()
            logger.info("TimescaleDB schema checked and initialized.")
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            logger.error("Error during schema initialization: %s", exc)
        finally:
            self.pool.putconn(conn)

    # ── Writes ──────────────────────────────────────────────────────────
    def write_episodic_memory(
        self,
        memory_id: str,
        tenant_id: str,
        agent_role: str,
        summary: str,
        importance: float,
        embedding: list[float],
        context: dict[str, Any] | None = None,
    ) -> None:
        """Insert a new episodic memory node."""
        now = datetime.now(timezone.utc)

        if not self.pool_active:
            self.fallback_storage.append(
                {
                    "memory_id": memory_id,
                    "tenant_id": tenant_id,
                    "created_at": now,
                    "last_retrieved_at": now,
                    "agent_role": agent_role,
                    "summary": summary,
                    "importance_score": importance,
                    "retrieval_count": 0,
                    "embedding": embedding,
                    "context_data": context,
                }
            )
            logger.info("[In-Memory Store] Added episodic memory: '%s'", summary)
            return

        conn = self.pool.getconn()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO episodic_memories (
                        memory_id, tenant_id, created_at, last_retrieved_at,
                        agent_role, summary, context_data, importance_score,
                        retrieval_count, embedding
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                    """,
                    (
                        memory_id,
                        tenant_id,
                        now,
                        now,
                        agent_role,
                        summary,
                        json.dumps(context or {}),
                        importance,
                        0,
                        self._bind_embedding(embedding),
                    ),
                )
                conn.commit()
            logger.info("[TimescaleDB] Added episodic memory: '%s'", summary)
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            logger.error("Failed to write memory to TimescaleDB: %s", exc)
        finally:
            self.pool.putconn(conn)

    # ── Reads ───────────────────────────────────────────────────────────
    def retrieve_similar_memories(
        self, tenant_id: str, query_embedding: list[float], limit: int = 5
    ) -> list[dict[str, Any]]:
        """Return the *limit* most cosine-similar memories for a tenant."""
        if not self.pool_active:
            return self._fallback_similarity(tenant_id, query_embedding, limit)

        conn = self.pool.getconn()
        memories: list[dict[str, Any]] = []
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT memory_id, created_at, last_retrieved_at, agent_role,
                           summary, importance_score, retrieval_count, embedding, context_data
                    FROM episodic_memories
                    WHERE tenant_id = %s
                    ORDER BY embedding <=> %s
                    LIMIT %s;
                    """,
                    (tenant_id, self._bind_embedding(query_embedding), limit),
                )
                memories = [self._row_to_dict(r) for r in cursor.fetchall()]
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to query vector similarity in TimescaleDB: %s", exc)
        finally:
            self.pool.putconn(conn)
        return memories

    def get_all_memories(self, tenant_id: str) -> list[dict[str, Any]]:
        """Retrieve every episodic memory for a tenant (used during consolidation)."""
        if not self.pool_active:
            return [m for m in self.fallback_storage if m["tenant_id"] == tenant_id]

        conn = self.pool.getconn()
        memories: list[dict[str, Any]] = []
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT memory_id, created_at, last_retrieved_at, agent_role,
                           summary, importance_score, retrieval_count, embedding, context_data
                    FROM episodic_memories
                    WHERE tenant_id = %s;
                    """,
                    (tenant_id,),
                )
                memories = [self._row_to_dict(r) for r in cursor.fetchall()]
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to retrieve all memories from TimescaleDB: %s", exc)
        finally:
            self.pool.putconn(conn)
        return memories

    # ── Helpers ─────────────────────────────────────────────────────────
    @staticmethod
    def _row_to_dict(r: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "memory_id": str(r[0]),
            "created_at": r[1],
            "last_retrieved_at": r[2],
            "agent_role": r[3],
            "summary": r[4],
            "importance_score": r[5],
            "retrieval_count": r[6],
            "embedding": list(r[7]) if r[7] is not None else [],
            "context_data": r[8],
        }

    def _fallback_similarity(
        self, tenant_id: str, query_embedding: list[float], limit: int
    ) -> list[dict[str, Any]]:
        scored: list[tuple[dict[str, Any], float]] = [
            (item, _cosine_similarity(item["embedding"], query_embedding))
            for item in self.fallback_storage
            if item["tenant_id"] == tenant_id
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [item for item, _ in scored[:limit]]


def _cosine_similarity(u: list[float], v: list[float]) -> float:
    dot = sum(a * b for a, b in zip(u, v, strict=False))
    norm_u = math.sqrt(sum(a * a for a in u))
    norm_v = math.sqrt(sum(b * b for b in v))
    if norm_u == 0 or norm_v == 0:
        return 0.0
    return dot / (norm_u * norm_v)
