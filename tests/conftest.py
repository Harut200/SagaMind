"""
SagaMind Test Suite — Shared Fixtures
=====================================

Provides reusable mocks and sample data for every test module.
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

from src.models import ActionPayload, SagaStep, MemoryNode, SagaStatus, StepStatus
from src.orchestrator.coordinator import SagaTransactionCoordinator


# ─────────────────────────────────────────────────────────────────────
# Verifier Fixtures
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_verifier():
    """Z3Verifier-like object where verify() always passes."""
    v = MagicMock()
    v.verify = MagicMock(return_value=(True, "OK"))
    return v


@pytest.fixture
def failing_verifier():
    """Z3Verifier-like object where verify() always fails."""
    v = MagicMock()
    v.verify = MagicMock(return_value=(False, "Violation"))
    return v


# ─────────────────────────────────────────────────────────────────────
# Sandbox Fixture
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_sandbox():
    """WasmSandbox-like object with successful execute/compensation."""
    sb = MagicMock()
    sb.execute = MagicMock(return_value={"status": "SUCCESS"})
    sb.execute_compensation = MagicMock(return_value=True)
    return sb


# ─────────────────────────────────────────────────────────────────────
# Coordinator Fixture
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def coordinator(mock_verifier, mock_sandbox):
    """Fully wired SagaTransactionCoordinator with mocked dependencies."""
    return SagaTransactionCoordinator(mock_verifier, mock_sandbox)


# ─────────────────────────────────────────────────────────────────────
# Sample Domain Objects
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_steps():
    """Three SagaStep instances with forward actions and compensations."""
    return [
        SagaStep(
            step_id="step-001",
            step_name="Create config file",
            action=ActionPayload(
                tool_name="WRITE_FILE",
                arguments={"path": "/Users/Harutyun/Desktop/Portfolio1/config.yml", "content": "key: value"},
            ),
            compensation=ActionPayload(
                tool_name="DELETE_FILE",
                arguments={"path": "/Users/Harutyun/Desktop/Portfolio1/config.yml"},
            ),
            invariants='(assert (str.prefixof "/Users/Harutyun/Desktop/Portfolio1" path))',
        ),
        SagaStep(
            step_id="step-002",
            step_name="Insert database record",
            action=ActionPayload(
                tool_name="DATABASE_QUERY",
                arguments={"query": "INSERT INTO agents VALUES (1, 'alpha')"},
            ),
            compensation=ActionPayload(
                tool_name="DATABASE_QUERY",
                arguments={"query": "DELETE FROM agents WHERE id = 1"},
            ),
            invariants="",
        ),
        SagaStep(
            step_id="step-003",
            step_name="Write output log",
            action=ActionPayload(
                tool_name="WRITE_FILE",
                arguments={"path": "/Users/Harutyun/Desktop/Portfolio1/output.log", "content": "done"},
            ),
            compensation=ActionPayload(
                tool_name="DELETE_FILE",
                arguments={"path": "/Users/Harutyun/Desktop/Portfolio1/output.log"},
            ),
            invariants='(assert (str.prefixof "/Users/Harutyun/Desktop/Portfolio1" path))',
        ),
    ]


@pytest.fixture
def sample_memory_nodes():
    """Three MemoryNode instances with varying importance and retrieval counts."""
    now = datetime.now(timezone.utc)
    return [
        MemoryNode(
            memory_id="mem-001",
            created_at=now - timedelta(hours=1),
            last_retrieved_at=now - timedelta(minutes=5),
            agent_role="Planner",
            summary="Formulated task execution plan for deployment pipeline.",
            importance_score=0.9,
            retrieval_count=5,
            embedding=[0.1, 0.2, 0.3],
        ),
        MemoryNode(
            memory_id="mem-002",
            created_at=now - timedelta(days=7),
            last_retrieved_at=now - timedelta(days=6),
            agent_role="Executor",
            summary="Ran initial database migration scripts.",
            importance_score=0.3,
            retrieval_count=1,
            embedding=[0.4, 0.5, 0.6],
        ),
        MemoryNode(
            memory_id="mem-003",
            created_at=now - timedelta(hours=12),
            last_retrieved_at=now - timedelta(hours=10),
            agent_role="Critic",
            summary="Validated output of static analysis scan.",
            importance_score=0.7,
            retrieval_count=3,
            embedding=[0.7, 0.8, 0.9],
        ),
    ]
