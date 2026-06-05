"""
SagaMind Core Runtime API Server
================================

FastAPI gateway exposing the saga transaction lifecycle, memory consolidation/retrieval,
speculative execution, and health/readiness probes.

Security posture
----------------
* API-key authentication is enforced on all mutating/data endpoints whenever keys are
  configured (always in production); ``/health`` and ``/metrics`` are public.
* CORS is restricted to a configured allow-list.
* Request bodies are size-limited.
* Optional per-client rate limiting.
* Per-tool argument schema validation before sandbox execution.
* Request-ID header injected and propagated through all log lines.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, Literal

import uvicorn
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from src.config import settings
from src.logging_config import configure_logging, get_logger
from src.memory.consolidation import MemoryConsolidator
from src.memory.decay import EbbinghausMemoryManager
from src.memory.embedding import EmbeddingService
from src.memory.neo4j_store import Neo4jGraphStore
from src.memory.timescale_store import TimescaleMemoryStore
from src.models import ActionPayload, MemoryNode, SagaStep
from src.observability import metrics
from src.orchestrator.coordinator import CoordinatorError, SagaTransactionCoordinator
from src.orchestrator.sandbox import WasmSandbox
from src.orchestrator.state_store import SagaStateStore
from src.security import rate_limiter
from src.speculative.orchestrator import SpeculativeOrchestrator
from src.verifier.z3_prover import Z3Verifier

configure_logging()
logger = get_logger("SagaMind.API")

# APScheduler for automatic sleep-cycle consolidation (optional dependency).
_scheduler: Any = None
try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    _scheduler = AsyncIOScheduler()
except ImportError:
    pass

# ─────────────────────────────────────────────────────────────────────
# Request-ID context variable (propagated through all log lines)
# ─────────────────────────────────────────────────────────────────────

_request_id: ContextVar[str] = ContextVar("request_id", default="")

# ─────────────────────────────────────────────────────────────────────
# Service singletons
# ─────────────────────────────────────────────────────────────────────

verifier = Z3Verifier()
sandbox = WasmSandbox()
timescale = TimescaleMemoryStore()
neo4j = Neo4jGraphStore()
saga_store = SagaStateStore()
coordinator = SagaTransactionCoordinator(verifier, sandbox, db_client=saga_store)
memory_manager = EbbinghausMemoryManager()
consolidator = MemoryConsolidator(timescale, neo4j)
embedding_service = EmbeddingService()
speculative = SpeculativeOrchestrator(sandbox)


# ─────────────────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
    logger.info(
        "SagaMind API starting (env=%s, auth=%s, saga_store=%s).",
        settings.env,
        settings.auth_enabled,
        saga_store.backend,
    )
    try:
        recovered = coordinator.recover()
        if recovered:
            logger.warning("Recovered (rolled back) %d incomplete saga(s) on startup.", recovered)
    except Exception as exc:  # noqa: BLE001
        logger.error("Saga recovery on startup failed: %s", exc)

    if _scheduler is not None and settings.consolidation_cron:
        try:
            minute, hour, day, month, day_of_week = settings.consolidation_cron.split()
            _scheduler.add_job(
                func=lambda: consolidator.run_consolidation_cycle("*"),
                trigger="cron",
                minute=minute,
                hour=hour,
                day=day,
                month=month,
                day_of_week=day_of_week,
                id="sleep_cycle",
                replace_existing=True,
            )
            _scheduler.start()
            logger.info("Sleep-cycle scheduler started (cron=%s).", settings.consolidation_cron)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to start consolidation scheduler: %s", exc)

    try:
        yield
    finally:
        if _scheduler is not None and _scheduler.running:
            _scheduler.shutdown(wait=False)
        for name, closer in (("Neo4j", neo4j.close), ("saga store", saga_store.close)):
            try:
                closer()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error closing %s on shutdown: %s", name, exc)
        logger.info("SagaMind API shut down cleanly.")


app = FastAPI(
    title="SagaMind Core Runtime API",
    description="Enterprise Multi-Agent Transaction Runtime and Cognitive Memory Engine",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

if settings.cors_origin_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.middleware("http")
async def inject_request_id(request: Request, call_next: Any) -> Any:
    """Stamp every request with a correlation ID visible in all downstream log lines."""
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    token = _request_id.set(rid)
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    _request_id.reset(token)
    return response


@app.middleware("http")
async def limit_body_size(request: Request, call_next: Any) -> Any:
    """Reject oversized request bodies before they are buffered."""
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > settings.max_request_bytes:
        return JSONResponse(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            content={"detail": "Request body too large."},
        )
    return await call_next(request)


# ─────────────────────────────────────────────────────────────────────
# Security dependencies
# ─────────────────────────────────────────────────────────────────────


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if not settings.auth_enabled:
        return
    if not x_api_key or x_api_key not in settings.api_key_set:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )


def enforce_rate_limit(request: Request, x_api_key: str | None = Header(default=None)) -> None:
    client_key = x_api_key or (request.client.host if request.client else "anonymous")
    if not rate_limiter.allow(client_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Try again shortly.",
        )


_PROTECTED = [Depends(require_api_key), Depends(enforce_rate_limit)]


# ─────────────────────────────────────────────────────────────────────
# Per-tool argument schemas (validation before sandbox execution)
# ─────────────────────────────────────────────────────────────────────


class WriteFileArgs(BaseModel):
    path: str
    content: str = ""

    @field_validator("path")
    @classmethod
    def path_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("path must not be empty")
        return v


class DatabaseQueryArgs(BaseModel):
    table: str
    operation: Literal["SELECT", "INSERT", "UPDATE", "DELETE"]
    filters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("table")
    @classmethod
    def table_alphanum(cls, v: str) -> str:
        if not v.replace("_", "").isalnum():
            raise ValueError("table name must be alphanumeric (underscores allowed)")
        return v


class NoopArgs(BaseModel):
    pass


_TOOL_ARG_SCHEMAS: dict[str, type[BaseModel]] = {
    "WRITE_FILE": WriteFileArgs,
    "DATABASE_QUERY": DatabaseQueryArgs,
    "NOOP": NoopArgs,
    "DELETE_FILE": WriteFileArgs,
}

_ALLOWED_TOOLS = frozenset(_TOOL_ARG_SCHEMAS)


def _validate_tool_args(tool_name: str, arguments: dict[str, Any]) -> None:
    """Validate tool arguments against the registered schema. Raises HTTPException on failure."""
    if tool_name not in _ALLOWED_TOOLS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown tool '{tool_name}'. Allowed: {sorted(_ALLOWED_TOOLS)}",
        )
    schema = _TOOL_ARG_SCHEMAS[tool_name]
    try:
        schema(**arguments)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid arguments for tool '{tool_name}': {exc}",
        ) from exc


# ─────────────────────────────────────────────────────────────────────
# Request / Response schemas
# ─────────────────────────────────────────────────────────────────────


class StartSagaRequest(BaseModel):
    tenant_id: str
    goal: str


class StepProposal(BaseModel):
    saga_id: str
    step_name: str
    tool_name: str
    arguments: dict[str, Any]
    compensation_tool: str
    compensation_arguments: dict[str, Any]
    invariants: str
    idempotency_key: str | None = None


class DraftProposal(BaseModel):
    command: str
    arguments: dict[str, Any] = {}


class SpeculativeRequest(BaseModel):
    drafts: list[DraftProposal]


class HealthResponse(BaseModel):
    status: str
    environment: str
    version: str
    backends: dict[str, str] | None = None


# ─────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────


@app.get("/health", response_model=HealthResponse)
def health_check() -> HealthResponse:
    """Liveness + readiness probe reporting per-backend mode."""
    backends = {
        "timescale": "live" if getattr(timescale, "pool_active", False) else "fallback",
        "neo4j": "live" if getattr(neo4j, "active", False) else "fallback",
        "verifier": "z3" if getattr(verifier, "z3_active", False) else "semantic-fallback",
        "wasm": "live" if getattr(sandbox, "engine", None) else "host-fallback",
        "saga_store": saga_store.backend,
    }
    return HealthResponse(
        status="HEALTHY",
        environment=settings.env,
        version="1.0.0",
        backends=backends,
    )


@app.get("/metrics")
def prometheus_metrics() -> Response:
    """Prometheus scrape endpoint (public)."""
    payload, content_type = metrics.exposition()
    return Response(content=payload, media_type=content_type)


@app.post("/saga/start", dependencies=_PROTECTED)
def start_saga(payload: StartSagaRequest) -> dict[str, str]:
    """Initialize a new saga transaction session."""
    saga_id = str(uuid.uuid4())
    coordinator.start_transaction_log(saga_id, payload.goal, payload.tenant_id)
    return {"saga_id": saga_id, "status": "RUNNING"}


@app.post("/saga/step", dependencies=_PROTECTED)
def submit_step(payload: StepProposal) -> dict[str, str]:
    """Submit and execute a single saga step through the verification gate."""
    _validate_tool_args(payload.tool_name, payload.arguments)
    _validate_tool_args(payload.compensation_tool, payload.compensation_arguments)

    step = SagaStep(
        step_id=str(uuid.uuid4()),
        step_name=payload.step_name,
        action=ActionPayload(payload.tool_name, payload.arguments),
        compensation=ActionPayload(payload.compensation_tool, payload.compensation_arguments),
        invariants=payload.invariants,
        idempotency_key=payload.idempotency_key,
    )

    try:
        success = coordinator.execute_saga(payload.saga_id, [step])
    except CoordinatorError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "Transaction step validation failed. Saga rolled back.",
                "step_error": step.error,
            },
        )
    return {"status": "COMMITTED", "step_id": step.step_id}


@app.get("/saga/{saga_id}/status", dependencies=_PROTECTED)
def saga_status(saga_id: str) -> dict[str, Any]:
    """Return the current state of a saga transaction."""
    state = coordinator.get_saga_status(saga_id)
    if state is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Saga not found.")
    return state


@app.get("/saga/dead-letters", dependencies=_PROTECTED)
def list_dead_letters() -> dict[str, Any]:
    """Return sagas that reached COMPENSATION_FAILED and require manual operator resolution."""
    if not hasattr(saga_store, "list_dead_letters"):
        return {"dead_letters": []}
    return {"dead_letters": saga_store.list_dead_letters()}


@app.post("/memory/consolidate", dependencies=_PROTECTED)
def run_consolidation(tenant_id: str, background_tasks: BackgroundTasks) -> dict[str, str]:
    """Trigger an asynchronous memory consolidation sleep-cycle."""
    background_tasks.add_task(consolidator.run_consolidation_cycle, tenant_id)
    return {"status": "QUEUED", "message": "Asynchronous sleep cycle triggered."}


@app.get("/memory/active", dependencies=_PROTECTED)
def get_active_memories(
    tenant_id: str,
    query: str | None = None,
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Retrieve active (non-evicted) memories for a tenant, optionally ranked by *query*."""
    query_vector = embedding_service.embed(query) if query else [0.0] * settings.embedding_dim
    memories = timescale.retrieve_similar_memories(tenant_id, query_vector, limit=limit, offset=offset)

    active = []
    for m in memories:
        node = MemoryNode(
            memory_id=m["memory_id"],
            created_at=m["created_at"],
            last_retrieved_at=m["last_retrieved_at"],
            agent_role=m["agent_role"],
            summary=m["summary"],
            importance_score=m["importance_score"],
            retrieval_count=m["retrieval_count"],
            embedding=m.get("embedding", []),
        )
        if memory_manager.calculate_retention(node) >= memory_manager.tau:
            active.append(m)
    return {
        "active_memories": active,
        "limit": limit,
        "offset": offset,
        "count": len(active),
    }


@app.post("/speculative/run", dependencies=_PROTECTED)
async def run_speculative(payload: SpeculativeRequest) -> dict[str, Any]:
    """Validate candidate drafts in parallel and commit the first valid one."""
    drafts = [d.model_dump() for d in payload.drafts]
    results = await speculative.run_speculative_drafts(drafts)
    committed = speculative.select_and_commit(results)
    return {"results": results, "committed_sandbox": committed}


# ─────────────────────────────────────────────────────────────────────
# Server entry point
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("Starting SagaMind API on %s:%s", settings.host, settings.port)
    uvicorn.run(
        "src.main:app",
        host=settings.host,
        port=settings.port,
        reload=(settings.env == "development"),
        log_level="info",
    )
