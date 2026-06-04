import json
import logging
import math
from datetime import datetime, timezone
from typing import Any

from src.config import settings

logger = logging.getLogger("SagaMind.Memory.Timescale")

class TimescaleMemoryStore:
    """
    Episodic memory manager interfacing with TimescaleDB + pgvector.
    Supports relational queries and cosine similarity search.
    """
    def __init__(self):
        self.conn = None
        self.pool_active = False
        self.fallback_storage: list[dict[str, Any]] = []

        try:
            import psycopg2
            # Attempt to establish connections pool
            self.pool = psycopg2.pool.SimpleConnectionPool(
                1, 10,
                host=settings.db_host,
                port=settings.db_port,
                database=settings.db_name,
                user=settings.db_user,
                password=settings.db_pass
            )
            self.pool_active = True
            self.initialize_schema()
            logger.info("TimescaleDB connection pool successfully initialized.")
        except Exception as e:
            logger.warning(f"Database server connection failed. Initializing in-memory fallback store: {str(e)}")

    def initialize_schema(self):
        """Creates tables, hypertables, and vector indexes if they don't exist."""
        conn = self.pool.getconn()
        try:
            with conn.cursor() as cursor:
                # Enable vector extension
                cursor.execute("CREATE EXTENSION IF NOT EXISTS vector;")
                # Execute table creations
                cursor.execute("""
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
                        embedding VECTOR(1536)
                    );
                """)
                conn.commit()
            logger.info("TimescaleDB schema checked and initialized.")
        except Exception as e:
            conn.rollback()
            logger.error(f"Error during schema initialization: {str(e)}")
        finally:
            self.pool.putconn(conn)

    def write_episodic_memory(self, memory_id: str, tenant_id: str, agent_role: str, summary: str, importance: float, embedding: list[float], context: dict[str, Any] | None = None):
        """Inserts a new episodic memory node."""
        now = datetime.now(timezone.utc)

        if not self.pool_active:
            self.fallback_storage.append({
                "memory_id": memory_id,
                "tenant_id": tenant_id,
                "created_at": now,
                "last_retrieved_at": now,
                "agent_role": agent_role,
                "summary": summary,
                "importance_score": importance,
                "retrieval_count": 0,
                "embedding": embedding,
                "context_data": context
            })
            logger.info(f"[In-Memory Store] Added episodic memory: '{summary}'")
            return

        conn = self.pool.getconn()
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO episodic_memories (memory_id, tenant_id, created_at, last_retrieved_at, agent_role, summary, context_data, importance_score, retrieval_count, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """, (memory_id, tenant_id, now, now, agent_role, summary, json.dumps(context or {}), importance, 0, embedding))
                conn.commit()
            logger.info(f"[TimescaleDB] Added episodic memory: '{summary}'")
        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to write memory to TimescaleDB: {str(e)}")
        finally:
            self.pool.putconn(conn)

    def retrieve_similar_memories(self, tenant_id: str, query_embedding: list[float], limit: int = 5) -> list[dict[str, Any]]:
        """Queries episodic memories using cosine distance calculations."""
        if not self.pool_active:
            # Fallback cosine distance search in-memory
            results = []
            for item in self.fallback_storage:
                if item["tenant_id"] != tenant_id:
                    continue
                # Calculate cosine similarity
                u = item["embedding"]
                v = query_embedding
                dot = sum(a*b for a, b in zip(u, v, strict=True))
                norm_u = math.sqrt(sum(a*a for a in u))
                norm_v = math.sqrt(sum(b*b for b in v))
                similarity = (dot / (norm_u * norm_v)) if (norm_u > 0 and norm_v > 0) else 0.0
                results.append((item, similarity))
            # Sort by similarity score descending
            results.sort(key=lambda x: x[1], reverse=True)
            return [x[0] for x in results[:limit]]

        conn = self.pool.getconn()
        memories = []
        try:
            with conn.cursor() as cursor:
                # Query using pgvector cosine operator <=>
                cursor.execute("""
                    SELECT memory_id, created_at, last_retrieved_at, agent_role, summary, importance_score, retrieval_count, embedding, context_data
                    FROM episodic_memories
                    WHERE tenant_id = %s
                    ORDER BY embedding <=> %s
                    LIMIT %s;
                """, (tenant_id, query_embedding, limit))

                rows = cursor.fetchall()
                for r in rows:
                    memories.append({
                        "memory_id": r[0],
                        "created_at": r[1],
                        "last_retrieved_at": r[2],
                        "agent_role": r[3],
                        "summary": r[4],
                        "importance_score": r[5],
                        "retrieval_count": r[6],
                        "embedding": list(r[7]),
                        "context_data": r[8]
                    })
        except Exception as e:
            logger.error(f"Failed to query vector similarity in TimescaleDB: {str(e)}")
        finally:
            self.pool.putconn(conn)
        return memories

    def get_all_memories(self, tenant_id: str) -> list[dict[str, Any]]:
        """Retrieves all episodic memories for a specific tenant (used in consolidation)."""
        if not self.pool_active:
            return [m for m in self.fallback_storage if m["tenant_id"] == tenant_id]

        conn = self.pool.getconn()
        memories = []
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT memory_id, created_at, last_retrieved_at, agent_role, summary, importance_score, retrieval_count, embedding, context_data
                    FROM episodic_memories
                    WHERE tenant_id = %s;
                """, (tenant_id,))
                rows = cursor.fetchall()
                for r in rows:
                    memories.append({
                        "memory_id": str(r[0]),
                        "created_at": r[1],
                        "last_retrieved_at": r[2],
                        "agent_role": r[3],
                        "summary": r[4],
                        "importance_score": r[5],
                        "retrieval_count": r[6],
                        "embedding": list(r[7]),
                        "context_data": r[8]
                    })
        except Exception as e:
            logger.error(f"Failed to retrieve all memories from TimescaleDB: {str(e)}")
        finally:
            self.pool.putconn(conn)
        return memories
