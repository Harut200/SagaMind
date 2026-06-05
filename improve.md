# improve.md — Path to Production

A full end-to-end review of SagaMind: what is real, what is a stub, what must be built or fixed
before this is production-grade, where a faster language buys you something, and which features
would raise it from "impressive demo" to "deployable system."

Status today: **79/79 tests pass, but they are entirely mock-based.** No external service is
actually exercised. The architecture is sound and the code is clean; the gap is that most of the
"hard" integrations degrade to in-memory fallbacks, and a few headline features are stubs.

Legend: **[BUG]** real defect · **[STUB]** advertised but not implemented · **[GAP]** missing for
production · **[PERF]** speed opportunity · **[FEAT]** new capability.

---

## 0. Severity-ordered summary

| # | Item | Type | Effort |
|---|------|------|--------|
| 1 | `psycopg2.pool` never imported → DB always falls back to memory | BUG | XS |
| 2 | WASM sandbox performs no isolation; writes straight to host disk | STUB/SEC | L |
| 3 | Z3 verifier ignores arbitrary invariants (only `str.prefixof`/`path`) | STUB | M |
| 4 | No API authentication / authorization / rate limiting | GAP/SEC | M |
| 5 | Committed default secrets; no fail-closed in production | GAP/SEC | S |
| 6 | Saga state is in-process only; lost on restart (Redis unused) | GAP | M |
| 7 | gRPC surface entirely missing despite deps/ports | STUB | M |
| 8 | Speculative execution is an `asyncio.sleep` simulation, unwired | STUB | M |
| 9 | Consolidation: O(n^2) pure-Python clustering; LLM distill unwired | STUB/PERF | M |
| 10 | No embedding generation (OpenAI client never instantiated) | GAP | S |
| 11 | No CI, no coverage gate, no pre-commit, no integration tests | GAP | M |
| 12 | `app_demo` decay path mixes naive/aware datetimes | BUG | XS |
| 13 | Vector/decay math in pure Python — vectorize or move to a fast lang | PERF | M–L |

---

## 1. Correctness bugs to fix first (cheap, high value)

### 1.1 [BUG] TimescaleDB connection is dead code
`src/memory/timescale_store.py` does `import psycopg2` then calls
`psycopg2.pool.SimpleConnectionPool(...)`. `psycopg2.pool` is **not** auto-imported, so this
raises `AttributeError`, which the broad `except Exception` swallows — the store silently uses
its in-memory list **even when the database is up and reachable**.

Fix:
```python
import psycopg2
from psycopg2 import pool          # <-- add
...
self.pool = pool.SimpleConnectionPool(1, 10, ...)
```
Then narrow the `except` (see 1.4) so a real connection error is visible, not masked.

### 1.2 [BUG] Embedding column binding for pgvector
The insert/query pass a Python `list[float]` directly to `embedding VECTOR(1536)`. With
`psycopg2` you must register a pgvector adapter (`pgvector.psycopg2.register_vector(conn)`) or
format the literal as `'[...]'`; otherwise inserts/queries fail. Add `pgvector` to deps and
register the type after each `getconn()`.

### 1.3 [BUG] Naive vs aware datetime in the dashboard
`app_demo.py` constructs `MemoryNode(..., datetime.now() - timedelta(...))` (naive), but
`EbbinghausMemoryManager.calculate_retention` computes `datetime.now(timezone.utc) - last_ret`,
which raises `TypeError: can't subtract offset-naive and offset-aware datetimes`. Use
`datetime.now(timezone.utc)` consistently, or normalize in `calculate_retention`.

### 1.4 [GAP] Stop swallowing every exception
Constructors use `except Exception` (and `sandbox.py` even `except (ImportError, Exception)`,
which is redundant). Catch the specific import/connection errors, log at WARNING that a fallback
engaged, and expose the live-vs-fallback state on `/health` so operators know what mode they are
in. Add an env flag `REQUIRE_BACKENDS=true` that makes the constructors raise instead of
degrade (production should fail closed).

### 1.5 [GAP] Dead/unused model
`SagaTransaction` dataclass is defined but the coordinator tracks state as a raw dict. Either use
the dataclass in `active_sagas` (better typing, serialization) or drop it. Same for the unused
`SandboxResult` typed return — `WasmSandbox.execute` returns bare dicts instead.

---

## 2. Security — must close before any real deployment

### 2.1 [STUB/SEC] The "sandbox" is not a sandbox
`WasmSandbox.execute` has an empty `pass` on the wasmtime path, then does real
`os.makedirs` + `open(path, "w")` on the **host**, guarded only by a `str.startswith` check.
This is path-prefix string security — trivially bypassable with symlinks, `..` segments that
still start with the prefix, or case/normalization tricks — and provides no CPU/memory/syscall
isolation. Options, strongest first:
- **Real WASM**: compile tools to WASI, run under `wasmtime` with `consume_fuel`, a memory
  limit, and a *preopened* directory so the guest physically cannot see outside the workspace.
- **MicroVM**: Firecracker / gVisor for untrusted tool code.
- **Container-per-step**: ephemeral rootless container with a read-only bind except a scratch
  mount.
At minimum, replace `startswith` with `os.path.realpath` + `os.path.commonpath` containment and
reject symlinks.

### 2.2 [GAP/SEC] No authentication on the API
`/saga/*` and `/memory/*` are open and can write files and trigger background work. Add:
- API-key or OAuth2/JWT bearer auth (FastAPI dependency).
- Per-tenant authorization (the `tenant_id` is currently trusted from the body).
- Rate limiting (slowapi / nginx) and request size limits.
- CORS allow-list (none configured today).
- Security headers, and disable `/docs` in production or gate it.

### 2.3 [SEC] Secrets management
`config.py`, `docker-compose.yml`, and `.env.example` all ship real-looking default passwords
(`sagamind_secure_pass_2026`, etc.) and the workspace root is hardcoded to a personal Desktop
path. Production only does `warnings.warn`. Required:
- Remove defaults for all secrets; **fail closed** in production if unset.
- Pull secrets from a vault / Docker secrets / cloud secret manager, never the image.
- Make `allowed_workspace_root` mandatory and validated (must exist, must be absolute, no
  default to a developer's machine).

### 2.4 [SEC] Container hardening
Dockerfile runs as `root` and `docker-compose` bind-mounts the whole repo over `/app` (defeats
the image). Add a non-root `USER`, drop the dev bind-mount in the prod compose, set
`read_only: true` + `cap_drop: [ALL]` + `no-new-privileges`, and pin base images by digest.

### 2.5 [GAP] Input validation on tool arguments
`StepProposal.arguments` is an open `dict[str, Any]`. Validate tool names against an allow-list
and validate per-tool argument schemas before they reach the sandbox or the SQL layer (the
`DATABASE_QUERY` tool currently takes raw SQL — never let that touch a real DB without
parameterization / a query allow-list).

---

## 3. Make the advertised features real

### 3.1 [STUB] Z3 verifier — general invariant support
Today `verify()` only injects a hardcoded `str.prefixof` constraint when the literal substring
`"str.prefixof"` appears and a `path` arg exists; the `invariants` SMT-LIB2 string is otherwise
ignored. To deliver "formal invariant proving":
- Parse the invariant with `z3.parse_smt2_string`, binding declared consts to the action args.
- Build the model as *assert args ∧ ¬invariant*; `sat` ⇒ violation (counter-example), `unsat`
  ⇒ safe. (The current sat/unsat polarity only works for the injected prefix case.)
- Define a small, documented invariant DSL/library (path containment, numeric bounds, string
  patterns) so callers don't hand-write SMT-LIB2.
- Add a verifier timeout (`solver.set("timeout", ms)`) and treat `unknown` as fail-closed.

### 3.2 [STUB] gRPC gateway
Deps, `grpc_port`, and `EXPOSE 50051` exist with no implementation. Either:
- Write `proto/sagamind.proto`, generate stubs, and run a `grpc.aio` server alongside FastAPI
  (saga start/step/status, memory ops, streaming step events), **or**
- Remove the gRPC deps/ports/claims until it's built (don't ship phantom surface area).

### 3.3 [STUB] Speculative execution
`SpeculativeOrchestrator` is an `asyncio.sleep(0.1)` mock, unwired and untested. To make the
"parallel COW sandbox, ~60% speedup" claim real:
- Back drafts with the real sandbox (3.x) using COW overlays (overlayfs / btrfs snapshot / WASM
  store fork) so drafts are isolated and cheap to discard.
- Add a selection policy (verify each draft, commit the first valid one, roll back the rest).
- Wire it into the coordinator behind a flag and **benchmark** the speedup instead of asserting
  it. Add tests.

### 3.4 [STUB] Memory consolidation & LLM distillation
- The clustering is a single O(n^2) pass, not DBSCAN (no `min_samples`/core-point density). Use
  `sklearn.cluster.DBSCAN(metric="cosine")` (sklearn is already a dashboard dep) or HDBSCAN.
- `self.llm` is stored and never used; cluster concepts are literally `f"Cluster {n} Concept"`.
  Call the configured `CONSOLIDATION_MODEL` to summarize each cluster into a real concept label
  and relationships, with batching and a deterministic offline fallback for tests.
- Persist consolidated concepts back as semantic memories, and add a scheduler (APScheduler /
  cron / Celery beat) so the "sleep cycle" runs periodically, not only on manual POST.

### 3.5 [GAP] Embedding pipeline
`OPENAI_API_KEY`/`EMBEDDING_MODEL` are configured but no client is ever created, and
`/memory/active` queries with a hardcoded `[0.1]*1536` dummy vector. Add an embedding service
(OpenAI or a local model via `sentence-transformers`) used at write time and at query time, with
caching and a deterministic stub for tests. Without this, vector search returns arbitrary order.

### 3.6 [GAP] Saga durability
`active_sagas` is an in-process dict; `db_client` is always `None`. A crash loses all in-flight
sagas and their compensation plans — the opposite of the transactional guarantee being sold.
Persist the saga log (the existing `db.write_transaction_state` hooks are already there) to
Postgres and/or Redis, and add crash recovery that resumes or compensates incomplete sagas on
startup. Add idempotency keys per step so retries don't double-execute.

### 3.7 [GAP] Neo4j store is write-only
`Neo4jGraphStore` can `upsert_relationship` but has no read/query methods, so the semantic graph
can never be retrieved or used in reasoning. Add concept lookup, neighborhood/subgraph queries,
and weight-decay maintenance.

---

## 4. Where a faster language actually helps (PERF)

Most of the codebase is I/O-bound glue where Python is fine. The real CPU hot spots are the
vector and decay math, currently written as pure-Python loops. Tiered plan, cheapest first:

### 4.1 Vectorize in NumPy (do this before reaching for another language)
- **Cosine similarity** in `timescale_store.retrieve_similar_memories` (fallback) and
  `consolidation.compute_cosine_distance` are Python `sum()`/`math.sqrt` loops over 1536-dim
  vectors. Replace with a single NumPy matrix op: stack embeddings into an `(N, 1536)` array,
  normalize once, and compute all pairwise similarities as one `A @ A.T`. This alone is ~100x
  for the clustering step and removes the O(n^2) Python overhead.
- **Ebbinghaus decay** over many memories: compute retention for the whole batch as vectorized
  NumPy (or push it into SQL — see 4.4) instead of a per-row Python loop.

### 4.2 Use the right algorithm/library before the right language
- Swap the hand-rolled clustering for `sklearn`/HDBSCAN (C-backed) — bigger win than rewriting
  the naive version in Rust.
- For production-scale retrieval, do **not** do similarity in Python at all: use pgvector's ANN
  index (HNSW/IVFFlat) in Postgres, or a dedicated vector DB (Qdrant/Milvus). This pushes the
  hot path into optimized C/Rust and scales past memory limits.

### 4.3 Native extensions where a tight loop remains (Rust / C / Cython)
If, after 4.1–4.2, a CPU loop is still the bottleneck (e.g. a custom distance metric over
millions of vectors, or a real COW diff/merge for speculative state):
- **Rust via PyO3 + maturin** is the best fit here — memory-safe, easy to package as a wheel,
  great for the distance-matrix kernel and for the sandbox/COW overlay logic. The WASM runtime
  you already want (`wasmtime`) is itself Rust.
- **Cython / C** if you want to stay in-tree and only need to hot-spot one function.
- Reach for SIMD (`numpy` already uses BLAS; Rust `std::simd` / `faer`) only for the proven hot
  kernel, behind a benchmark.

### 4.4 Push compute into the datastore
- Compute retention and prune candidates with a SQL expression in TimescaleDB (it's just
  `exp(-Δt/strength)`), so you never load every row into Python.
- Use TimescaleDB continuous aggregates / retention policies for the time-series eviction.

### 4.5 Service-boundary rewrites (only if throughput demands it)
- A high-QPS gRPC/HTTP **gateway** is a reasonable thing to write in **Go** (great concurrency,
  simple deploy) or Rust (axum/tonic), keeping the Python core as the reasoning engine behind it.
  Don't do this until a benchmark shows the Python/FastAPI layer is the bottleneck — `uvicorn`
  workers + async already go a long way.
- The sandbox/isolation layer is the other strong candidate for Rust (wasmtime, Firecracker
  bindings, COW filesystem control).

**Guidance:** profile first (`py-spy`, `scalene`). In order of ROI: NumPy vectorization →
right library/ANN index → push to SQL → Rust/PyO3 for the one proven kernel → service rewrite.
Don't rewrite the saga coordinator or FastAPI layer in another language; they're I/O-bound.

---

## 5. Production engineering gaps (GAP)

### 5.1 Testing
- All 79 tests are unit/mock. Add **integration tests** against real TimescaleDB/Neo4j/Redis via
  `testcontainers` or the existing compose, run in CI.
- Add property-based tests (Hypothesis) for the saga state machine (every failure point must end
  in a consistent terminal state) and for the decay math.
- Add a coverage gate (`pytest-cov`, fail under e.g. 85%). `pytest-asyncio` is installed but the
  speculative async path has no tests.
- Add a `py.typed` marker (the package advertises `Typing :: Typed`) and run `mypy` in CI
  (currently `disallow_untyped_defs = false` — tighten gradually).

### 5.2 CI/CD
- No `.github/workflows`. Add: lint (ruff) → type (mypy) → test (matrix on 3.10–3.13) →
  build image → security scan (pip-audit/trivy) → publish.
- Add `pre-commit` (ruff, end-of-file, secret scanning like `detect-secrets`/`gitleaks` — there
  are committed default credentials to catch).
- Dependabot / renovate for dependency updates.

### 5.3 Observability
- Logging is solid (structured JSON in prod). Add **metrics** (Prometheus: saga
  success/rollback/compensation-failure counts, step latency, verifier latency, decay job
  duration) and **tracing** (OpenTelemetry across API → verifier → sandbox).
- `/health` is static; add real readiness checks (DB/Neo4j/Redis pings) and a separate
  liveness vs readiness split. Surface fallback-mode status (see 1.4).
- Alert on `COMPENSATION_FAILED` — that's the "environment is now inconsistent" event.

### 5.4 Configuration & packaging
- `requirements.txt` duplicates `pyproject` dependencies (drift risk). Pick one source of truth;
  pin with a lockfile (`uv`/`pip-tools`/`poetry`) and use hashes.
- Migrate `Settings` to `pydantic-settings` for typed validation and `.env` parsing instead of
  manual `os.getenv` + `warnings.warn`.
- Add DB migrations (Alembic) instead of `CREATE TABLE IF NOT EXISTS` at runtime.
- Fix the misspelled doc filename `arcitecture_exp.md` (referenced from README) → `architecture`.
- Reconcile the Python-version story (README badge says 3.9/3.10/3.11; classifiers 3.10–3.12;
  `requires-python >=3.10`; running on 3.13).

### 5.5 Reliability
- Add timeouts and retries (with backoff) around every external call (DB, Neo4j, LLM, Z3).
- Add a dead-letter / manual-intervention path for `COMPENSATION_FAILED` sagas.
- Graceful shutdown: drain in-flight sagas, close pools/drivers (`Neo4jGraphStore.close` exists
  but is never called; register FastAPI lifespan handlers).
- Bound `active_sagas` growth (it never evicts committed sagas → memory leak in a long-running
  process).

### 5.6 Docs & governance
- README references "Contribution Guidelines" — add `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`,
  `CHANGELOG.md`, and a `Makefile`/`justfile` for the common commands.
- License mismatch: README badge + `pyproject` say MIT; confirm `LICENSE` matches.

---

## 6. Feature ideas (FEAT) — beyond parity

Once the above is solid, these differentiate the product:

- **Distributed sagas**: run steps across workers (Celery/Temporal/Dramatiq) with durable
  orchestration; Temporal in particular is a natural fit for saga + compensation semantics.
- **Human-in-the-loop gate**: pause a saga for approval before a high-risk step; resume/abort.
- **Pluggable tool registry**: declare tools (forward + compensation + invariant + arg schema)
  as plugins; the current hardcoded `WRITE_FILE`/`DATABASE_QUERY` branches don't scale.
- **Memory importance learning**: update `importance_score`/decay params from actual retrieval
  outcomes (reinforcement), not static values.
- **Graph reasoning**: use the Neo4j semantic graph in retrieval (GraphRAG-style) so consolidated
  concepts actually feed agent context.
- **Multi-tenancy hardening**: per-tenant quotas, isolation, encryption-at-rest, audit log.
- **WebSocket / SSE streaming** of saga step events to clients and the dashboard (currently the
  dashboard re-runs the engine locally instead of calling the API).
- **Policy engine** (OPA/Rego) alongside Z3 for non-mathematical authorization rules.
- **Replay & time-travel debugging**: persist the full step/compensation log and allow replaying
  a saga deterministically.
- **SDK/client library** (Python first) so users don't hand-roll HTTP/gRPC calls.

---

## 7. Suggested sequencing

1. **Week 1 — make it honest & correct**: fix 1.1–1.4 (DB import, pgvector, datetime, error
   handling), add `REQUIRE_BACKENDS`/health truthfulness, remove committed secrets, add basic
   API auth. Add integration tests + CI. *No new features.*
2. **Week 2 — real isolation & verification**: implement a genuine sandbox (4.3/3.1 WASM with
   preopened dir + fuel), and general Z3 invariant parsing. Persist saga state to Postgres/Redis
   with crash recovery.
3. **Week 3 — memory pipeline**: embeddings, sklearn/HDBSCAN consolidation, LLM distillation,
   scheduler, Neo4j read path, pgvector ANN index. NumPy-vectorize the math (Section 4.1).
4. **Week 4 — scale & polish**: gRPC (or drop it), speculative execution backed by real COW +
   benchmark, metrics/tracing, container hardening, docs. Reach for Rust/PyO3 only for a kernel
   that profiling proves is still hot (Section 4.3).

---

*Generated from a full E2E read of the repository (src/, tests/, app_demo.py, infra, docs).
See `CLAUDE.md` for day-to-day working conventions and the authoritative "what's real vs stub"
caveats.*
