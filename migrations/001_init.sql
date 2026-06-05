-- SagaMind initial schema (TimescaleDB + pgvector)
-- ================================================
-- Mounted into the TimescaleDB container's docker-entrypoint-initdb.d so the schema is
-- created on first boot. The application's runtime initialize_schema() is idempotent and
-- mirrors this file; managed migrations (Alembic) are the recommended next step — see
-- improve.md §5.4.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS timescaledb;

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

-- Time-series partitioning on encode time for retention policies and fast time scans.
SELECT create_hypertable('episodic_memories', 'created_at', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_episodic_tenant
    ON episodic_memories (tenant_id);

-- Approximate-nearest-neighbour index for cosine similarity retrieval.
CREATE INDEX IF NOT EXISTS idx_episodic_embedding
    ON episodic_memories USING hnsw (embedding vector_cosine_ops);

-- Durable saga transaction log (crash recovery / audit). The coordinator persists state
-- transitions here when wired with a db_client (see improve.md §3.6).
CREATE TABLE IF NOT EXISTS saga_transactions (
    saga_id      UUID PRIMARY KEY,
    tenant_id    VARCHAR(50) NOT NULL,
    goal         TEXT NOT NULL,
    status       VARCHAR(32) NOT NULL,
    metadata     JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_saga_tenant
    ON saga_transactions (tenant_id);
