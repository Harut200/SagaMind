"""
SagaMind Core Runtime API Server
=================================

Enterprise Multi-Agent Transaction Runtime and Cognitive Memory Engine.

Exposes RESTful endpoints for:
    - Saga transaction lifecycle management (start, step, status)
    - Memory consolidation and retrieval
    - Health monitoring
"""

import uvicorn
import logging
import uuid
from typing import Dict, Any, List

from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel

# Local SagaMind imports
from src.config import settings
from src.logging_config import configure_logging, get_logger
from src.models import ActionPayload, SagaStep, MemoryNode
from src.orchestrator.coordinator import SagaTransactionCoordinator
from src.orchestrator.sandbox import WasmSandbox
from src.verifier.z3_prover import Z3Verifier
from src.memory.timescale_store import TimescaleMemoryStore
from src.memory.neo4j_store import Neo4jGraphStore
from src.memory.decay import EbbinghausMemoryManager
from src.memory.consolidation import MemoryConsolidator

# Initialize logging
configure_logging()
logger = get_logger("SagaMind.API")

# ─────────────────────────────────────────────────────────────────────
# FastAPI Application
# ─────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="SagaMind Core Runtime API",
    description="Enterprise Multi-Agent Transaction Runtime and Cognitive Memory Engine",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ─────────────────────────────────────────────────────────────────────
# Service Initialization
# ─────────────────────────────────────────────────────────────────────

verifier = Z3Verifier()
sandbox = WasmSandbox()
timescale = TimescaleMemoryStore()
neo4j = Neo4jGraphStore()
coordinator = SagaTransactionCoordinator(verifier, sandbox, db_client=None)
memory_manager = EbbinghausMemoryManager()
consolidator = MemoryConsolidator(timescale, neo4j)

# ─────────────────────────────────────────────────────────────────────
# Request / Response Schemas (Pydantic)
# ─────────────────────────────────────────────────────────────────────

class StartSagaRequest(BaseModel):
    tenant_id: str
    goal: str


class StepProposal(BaseModel):
    saga_id: str
    step_name: str
    tool_name: str
    arguments: Dict[str, Any]
    compensation_tool: str
    compensation_arguments: Dict[str, Any]
    invariants: str


class HealthResponse(BaseModel):
    status: str
    environment: str
    version: str


# ─────────────────────────────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
def health_check():
    """Liveness and readiness probe."""
    return HealthResponse(
        status="HEALTHY",
        environment=settings.env,
        version="1.0.0"
    )


@app.post("/saga/start")
def start_saga(payload: StartSagaRequest):
    """Initialize a new saga transaction session."""
    saga_id = str(uuid.uuid4())
    coordinator.start_transaction_log(saga_id, payload.goal, payload.tenant_id)
    return {"saga_id": saga_id, "status": "RUNNING"}


@app.post("/saga/step")
def submit_step(payload: StepProposal):
    """Submit and execute a single saga step through the verification gate."""
    step = SagaStep(
        step_id=str(uuid.uuid4()),
        step_name=payload.step_name,
        action=ActionPayload(payload.tool_name, payload.arguments),
        compensation=ActionPayload(payload.compensation_tool, payload.compensation_arguments),
        invariants=payload.invariants
    )

    success = coordinator.execute_saga(payload.saga_id, [step])

    if not success:
        raise HTTPException(status_code=400, detail={
            "error": "Transaction step validation failed. Saga rolled back.",
            "step_error": step.error
        })

    return {"status": "COMMITTED", "step_id": step.step_id}


@app.post("/memory/consolidate")
def run_consolidation(tenant_id: str, background_tasks: BackgroundTasks):
    """Trigger asynchronous memory consolidation sleep-cycle."""
    background_tasks.add_task(consolidator.run_consolidation_cycle, tenant_id)
    return {"status": "QUEUED", "message": "Asynchronous sleep cycle triggered."}


@app.get("/memory/active")
def get_active_memories(tenant_id: str):
    """Retrieve active (non-evicted) memories for a tenant."""
    # Fetch consolidated vector similarities
    dummy_query_vector = [0.1] * 1536
    memories = timescale.retrieve_similar_memories(tenant_id, dummy_query_vector)

    # Filter using active Ebbinghaus decay limits
    active_m = []
    for m in memories:
        node = MemoryNode(
            memory_id=m["memory_id"],
            created_at=m["created_at"],
            last_retrieved_at=m["last_retrieved_at"],
            agent_role=m["agent_role"],
            summary=m["summary"],
            importance_score=m["importance_score"],
            retrieval_count=m["retrieval_count"],
            embedding=m.get("embedding", [])
        )
        r = memory_manager.calculate_retention(node)
        if r >= memory_manager.tau:
            active_m.append(m)

    return {"active_memories": active_m}


# ─────────────────────────────────────────────────────────────────────
# Server Entry Point
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info(f"Starting SagaMind API on {settings.host}:{settings.port}")
    uvicorn.run(
        "src.main:app",
        host=settings.host,
        port=settings.port,
        reload=(settings.env == "development"),
        log_level="info",
    )
