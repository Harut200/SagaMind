# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added (durability & operations)
- **Durable saga state** (`src/orchestrator/state_store.py`): `SagaStateStore` with
  Postgres → Redis → in-memory backend auto-detection (`STATE_STORE_BACKEND` to pin),
  a persisted compensation log, and `coordinator.recover()` that rolls back sagas left
  incomplete by a crash — run automatically on API startup. Redis is now actually used.
- **Observability** (`src/observability/`): Prometheus metrics (saga/step/compensation
  counters + verify/step latency histograms), a public `/metrics` endpoint, and an optional
  OpenTelemetry span helper. All degrade to no-ops when the libraries are absent.
- **Alembic migrations** (`alembic/`) with an initial revision mirroring the schema; raw SQL
  init (`migrations/001_init.sql`) retained for first-boot container provisioning.
- **Integration test suite** (`tests/integration/`) gated by `RUN_INTEGRATION=1`, exercising
  live TimescaleDB/Neo4j/Redis; skipped by default so the unit suite stays hermetic.
- **True WASM isolation primitive** `WasmSandbox.run_wasm_module` — fuel-metered, with only
  the workspace directory pre-opened (WASI), for executing untrusted compiled tool code.

### Fixed
- **Critical:** TimescaleDB pool was never used — `psycopg2.pool` submodule was not
  imported, so the constructor always raised and was silently swallowed. The store now
  imports the pool correctly, registers the pgvector adapter, and narrows exception
  handling so real connection failures are visible.
- Embedding values now bind through the pgvector adapter (or an explicit vector literal),
  so inserts and `<=>` cosine queries work.
- Decay computation normalises naive/aware datetimes, fixing a `TypeError` in the
  dashboard's retention chart.

### Added
- `src/config.py` migrated to `pydantic-settings` with **fail-closed** production
  validation (refuses to boot with default/empty secrets or no API keys).
- `src/security.py`: true filesystem-jail containment (`contain_path`) resolving symlinks
  and traversal, a fixed-window rate limiter, and reusable primitives.
- API authentication (API key), CORS allow-list, request-size limit, per-client rate
  limiting, readiness/health backend reporting, `GET /saga/{id}/status`, and a
  `POST /speculative/run` endpoint.
- General SMT-LIB2 invariant parsing in the Z3 verifier (refutation method) with a
  solver timeout that fails closed on `unknown`.
- Real path-jail + tool allow-list + fuel metering in the sandbox; typed `SandboxResult`.
- Side-effect-free parallel validation + winner-commit in the speculative orchestrator.
- Deterministic embedding service (`EmbeddingService`) with an OpenAI backend.
- Neo4j read APIs (`get_neighbors`, `get_all_relationships`).
- Vectorised (NumPy) cosine distance in consolidation and a batched decay path.
- gRPC surface: `proto/sagamind.proto`, async server, and codegen script.
- Infrastructure: non-root Dockerfile + healthcheck, fail-closed hardened
  `docker-compose.yml`, `.dockerignore`, SQL migrations, CI workflow, pre-commit hooks,
  `py.typed`, `Makefile`, and contributor docs.

### Changed
- `requirements.txt` reduced to the runtime subset; extras moved to `pyproject.toml`.
- Coordinator gains `get_saga_status` and bounded in-memory saga retention.

## [1.0.0]
- Initial public architecture: saga coordinator, Z3 gate, tiered memory, speculative
  execution, Streamlit dashboard.
