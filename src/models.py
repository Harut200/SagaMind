"""
SagaMind Core Domain Models
===========================

Canonical dataclass definitions shared across every subsystem.
All modules import from here — this is the single source of truth.

Design Rationale:
  - Dataclasses over Pydantic BaseModel for zero-overhead internal types.
  - Pydantic is used only at the API boundary (FastAPI request/response schemas).
  - Enum-driven state machines prevent stringly-typed status comparisons.
"""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

# ─────────────────────────────────────────────────────────────────────
# Enumerations
# ─────────────────────────────────────────────────────────────────────

class SagaStatus(str, enum.Enum):
    """Finite state machine for saga transaction lifecycle."""
    PENDING       = "PENDING"
    RUNNING       = "RUNNING"
    COMMITTED     = "COMMITTED"
    COMPENSATING  = "COMPENSATING"
    ROLLED_BACK   = "ROLLED_BACK"
    FAILED        = "FAILED"
    COMPENSATION_FAILED = "COMPENSATION_FAILED"


class StepStatus(str, enum.Enum):
    """Finite state machine for individual step lifecycle."""
    PENDING            = "PENDING"
    RUNNING            = "RUNNING"
    COMMITTED          = "COMMITTED"
    FAILED             = "FAILED"
    COMPENSATING       = "COMPENSATING"
    ROLLED_BACK        = "ROLLED_BACK"
    COMPENSATION_FAILED = "COMPENSATION_FAILED"


# ─────────────────────────────────────────────────────────────────────
# Action / Step Models
# ─────────────────────────────────────────────────────────────────────

@dataclass
class ActionPayload:
    """Describes a single tool invocation with its arguments."""
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class SagaStep:
    """
    Represents one atomic unit of work inside a saga transaction.

    Fields:
        step_id:       Globally unique identifier for this step.
        step_name:     Human-readable label for logging and dashboards.
        action:        The forward action to execute.
        compensation:  The reverse action to execute on rollback.
        invariants:    SMT-LIB2 assertion string for Z3 verification gate.
        status:        Current lifecycle state (enum-driven).
        error:         Error description if the step failed.
    """
    step_id: str
    step_name: str
    action: ActionPayload
    compensation: ActionPayload
    invariants: str
    status: str = StepStatus.PENDING.value
    error: str = ""


@dataclass
class SandboxResult:
    """Typed result from sandbox execution."""
    success: bool
    status: str = "SUCCESS"
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


# ─────────────────────────────────────────────────────────────────────
# Memory Models
# ─────────────────────────────────────────────────────────────────────

@dataclass
class MemoryNode:
    """
    A single episodic memory trace stored in the CLS memory system.

    Fields:
        memory_id:          UUID for this memory node.
        created_at:         Timestamp when the memory was first encoded.
        last_retrieved_at:  Timestamp of the most recent access.
        agent_role:         The agent persona that generated this memory.
        summary:            Human-readable description of the episodic event.
        importance_score:   Salience weight in [0.0, 1.0].
        retrieval_count:    Number of times this memory has been accessed.
        embedding:          Dense vector representation for similarity search.
    """
    memory_id: str
    created_at: datetime
    last_retrieved_at: datetime
    agent_role: str
    summary: str
    importance_score: float
    retrieval_count: int
    embedding: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary for storage and transport."""
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────
# Transaction Metadata
# ─────────────────────────────────────────────────────────────────────

@dataclass
class SagaTransaction:
    """
    Top-level metadata envelope for a saga transaction session.
    Tracks the full lifecycle including all completed steps.
    """
    saga_id: str
    tenant_id: str
    goal: str
    status: str = SagaStatus.RUNNING.value
    start_time: float = 0.0
    completed_steps: list[SagaStep] = field(default_factory=list)
