"""Initial SagaMind schema: episodic memories + saga transaction log.

Revision ID: 0001
Revises:
Create Date: 2026-06-05
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb;")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS episodic_memories (
            memory_id          UUID PRIMARY KEY,
            tenant_id          VARCHAR(50) NOT NULL,
            created_at         TIMESTAMPTZ NOT NULL,
            last_retrieved_at  TIMESTAMPTZ NOT NULL,
            agent_role         VARCHAR(50) NOT NULL,
            summary            TEXT NOT NULL,
            context_data       JSONB,
            importance_score   DOUBLE PRECISION NOT NULL,
            retrieval_count    INT NOT NULL DEFAULT 0,
            embedding          VECTOR(1536)
        );
        """
    )
    op.execute("SELECT create_hypertable('episodic_memories', 'created_at', if_not_exists => TRUE);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_episodic_tenant ON episodic_memories (tenant_id);")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_episodic_embedding "
        "ON episodic_memories USING hnsw (embedding vector_cosine_ops);"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS saga_transactions (
            saga_id    UUID PRIMARY KEY,
            tenant_id  VARCHAR(50),
            goal       TEXT,
            status     VARCHAR(32) NOT NULL,
            metadata   JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_saga_tenant ON saga_transactions (tenant_id);")
    op.execute(
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


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS saga_compensations;")
    op.execute("DROP TABLE IF EXISTS saga_transactions;")
    op.execute("DROP TABLE IF EXISTS episodic_memories;")
