"""
SagaMind Core Runtime API Server
================================

FastAPI gateway exposing the saga transaction lifecycle, memory consolidation/retrieval,
speculative execution, and health/readiness probes.

Security posture
----------------
* API-key authentication is enforced on all mutating/data endpoints whenever keys are
  configured (always in production); ``/health`` is public.
* CORS is restricted to a configured allow-list.
* Request bodies are size-limited.
* Optional per-client rate limiting.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.config import settings
from src.logging_config import configure_logging, get_logger
from src.memory.consolidation import MemoryConsolidator
from src.memory.decay import EbbinghausMemoryManager
from src.memory.embedding import EmbeddingService
from src.memory.neo4j_store import Neo4jGraphStore
from src.memory.timescale_store import TimescaleMemoryStore
from src.models import ActionPayload, MemoryNode, SagaStep
from src.observability import metrics
from src.orchestrator.coordinator import SagaTransactionCoordinator
from src.orchestrator.sandbox import WasmSandbox
from src.orchestrator.state_store import SagaStateStore
from src.security import rate_limiter
from src.speculative.orchestrator import SpeculativeOrchestrator
from src.verifier.z3_prover import Z3Verifier

configure_logging()
logger = get_logger("SagaMind.API")

# ─────────────────────────────────────────────────────────────────────
# Service Initialization (module singletons)
# ─────────────────────────────────────────────────────────────────────

verifier = Z3Verifier()
sandbox = WasmSandbox()
timescale = TimescaleMemoryStore()
neo4j = Neo4jGraphStore()
coordinator = SagaTransactionCoordinator(verifier, sandbox, db_client=None)
memory_manager = EbbinghausMemoryManager()
consolidator = MemoryConsolidator(timescale, neo4j)
embedding_service = EmbeddingService()
speculative = SpeculativeOrchestrator(sandbox)


# ─────────────────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
    logger.info("SagaMind API starting (env=%s, auth=%s).", settings.env, settings.auth_enabled)
    try:
        yield
    finally:
        try:
            neo4j.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error closing Neo4j driver on shutdown: %s", exc)
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
    """Enforce API-key auth when enabled; otherwise a no-op (development)."""
    if not settings.auth_enabled:
        return
    if not x_api_key or x_api_key not in settings.api_key_set:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )


def enforce_rate_limit(request: Request, x_api_key: str | None = Header(default=None)) -> None:
    """Apply per-client fixed-window rate limiting when configured."""
    client_key = x_api_key or (request.client.host if request.client else "anonymous")
    if not rate_limiter.allow(client_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Try again shortly.",
        )


_PROTECTED = [Depends(require_api_key), Depends(enforce_rate_limit)]


# ─────────────────────────────────────────────────────────────────────
# Schemas
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
    """Liveness + readiness probe reporting per-backend mode (live vs fallback)."""
    backends = {
        "timescale": "live" if getattr(timescale, "pool_active", False) else "fallback",
        "neo4j": "live" if getattr(neo4j, "active", False) else "fallback",
        "verifier": "z3" if getattr(verifier, "z3_active", False) else "semantic-fallback",
        "wasm": "live" if getattr(sandbox, "engine", None) else "host-fallback",
    }
    return HealthResponse(
        status="HEALTHY",
        environment=settings.env,
        version="1.0.0",
        backends=backends,
    )


@app.post("/saga/start", dependencies=_PROTECTED)
def start_saga(payload: StartSagaRequest) -> dict[str, str]:
    """Initialize a new saga transaction session."""
    saga_id = str(uuid.uuid4())
    coordinator.start_transaction_log(saga_id, payload.goal, payload.tenant_id)
    return {"saga_id": saga_id, "status": "RUNNING"}


@app.post("/saga/step", dependencies=_PROTECTED)
def submit_step(payload: StepProposal) -> dict[str, str]:
    """Submit and execute a single saga step through the verification gate."""
    step = SagaStep(
        step_id=str(uuid.uuid4()),
        step_name=payload.step_name,
        action=ActionPayload(payload.tool_name, payload.arguments),
        compensation=ActionPayload(payload.compensation_tool, payload.compensation_arguments),
        invariants=payload.invariants,
    )

    success = coordinator.execute_saga(payload.saga_id, [step])
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


@app.post("/memory/consolidate", dependencies=_PROTECTED)
def run_consolidation(tenant_id: str, background_tasks: BackgroundTasks) -> dict[str, str]:
    """Trigger an asynchronous memory consolidation sleep-cycle."""
    background_tasks.add_task(consolidator.run_consolidation_cycle, tenant_id)
    return {"status": "QUEUED", "message": "Asynchronous sleep cycle triggered."}


@app.get("/memory/active", dependencies=_PROTECTED)
def get_active_memories(tenant_id: str, query: str | None = None) -> dict[str, Any]:
    """Retrieve active (non-evicted) memories for a tenant, optionally ranked by *query*."""
    query_vector = embedding_service.embed(query) if query else [0.0] * settings.embedding_dim
    memories = timescale.retrieve_similar_memories(tenant_id, query_vector)

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
    return {"active_memories": active}


@app.post("/speculative/run", dependencies=_PROTECTED)
async def run_speculative(payload: SpeculativeRequest) -> dict[str, Any]:
    """Validate candidate drafts in parallel and commit the first valid one."""
    drafts = [d.model_dump() for d in payload.drafts]
    results = await speculative.run_speculative_drafts(drafts)
    committed = speculative.select_and_commit(results)
    return {"results": results, "committed_sandbox": committed}


# ─────────────────────────────────────────────────────────────────────
# Server Entry Point
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
