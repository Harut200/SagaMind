"""
SagaMind — Dataclass Model Tests
=================================

Validates construction, default values, serialization, and enum
definitions for every domain model used across the system.
"""

from datetime import datetime, timezone

import pytest

from src.models import (
    ActionPayload,
    MemoryNode,
    SagaStatus,
    SagaStep,
    SagaTransaction,
    SandboxResult,
    StepStatus,
)

# ─────────────────────────────────────────────────────────────────────
# ActionPayload
# ─────────────────────────────────────────────────────────────────────


class TestActionPayload:
    """Validate ActionPayload construction and defaults."""

    def test_construction_with_arguments(self):
        ap = ActionPayload(tool_name="WRITE_FILE", arguments={"path": "/tmp/x"})
        assert ap.tool_name == "WRITE_FILE"
        assert ap.arguments == {"path": "/tmp/x"}

    def test_default_arguments_is_empty_dict(self):
        ap = ActionPayload(tool_name="NOP")
        assert ap.arguments == {}

    def test_default_arguments_not_shared_across_instances(self):
        """Ensure dataclass field(default_factory=dict) creates independent dicts."""
        a = ActionPayload(tool_name="A")
        b = ActionPayload(tool_name="B")
        a.arguments["key"] = "val"
        assert "key" not in b.arguments


# ─────────────────────────────────────────────────────────────────────
# SagaStep
# ─────────────────────────────────────────────────────────────────────


class TestSagaStep:
    """Validate SagaStep construction and default status."""

    def test_construction(self):
        step = SagaStep(
            step_id="s1",
            step_name="test step",
            action=ActionPayload(tool_name="T"),
            compensation=ActionPayload(tool_name="C"),
            invariants="(assert true)",
        )
        assert step.step_id == "s1"
        assert step.step_name == "test step"
        assert step.action.tool_name == "T"
        assert step.compensation.tool_name == "C"
        assert step.invariants == "(assert true)"

    def test_default_status_is_pending(self):
        step = SagaStep(
            step_id="s2",
            step_name="s",
            action=ActionPayload(tool_name="T"),
            compensation=ActionPayload(tool_name="C"),
            invariants="",
        )
        assert step.status == StepStatus.PENDING.value
        assert step.status == "PENDING"

    def test_default_error_is_empty_string(self):
        step = SagaStep(
            step_id="s3",
            step_name="s",
            action=ActionPayload(tool_name="T"),
            compensation=ActionPayload(tool_name="C"),
            invariants="",
        )
        assert step.error == ""

    def test_status_can_be_overridden(self):
        step = SagaStep(
            step_id="s4",
            step_name="s",
            action=ActionPayload(tool_name="T"),
            compensation=ActionPayload(tool_name="C"),
            invariants="",
            status=StepStatus.COMMITTED.value,
        )
        assert step.status == "COMMITTED"


# ─────────────────────────────────────────────────────────────────────
# MemoryNode
# ─────────────────────────────────────────────────────────────────────


class TestMemoryNode:
    """Validate MemoryNode construction and serialization."""

    @pytest.fixture
    def node(self):
        now = datetime.now(timezone.utc)
        return MemoryNode(
            memory_id="m1",
            created_at=now,
            last_retrieved_at=now,
            agent_role="Planner",
            summary="Test memory",
            importance_score=0.85,
            retrieval_count=3,
            embedding=[0.1, 0.2, 0.3],
        )

    def test_construction(self, node):
        assert node.memory_id == "m1"
        assert node.agent_role == "Planner"
        assert node.importance_score == 0.85
        assert node.retrieval_count == 3

    def test_default_embedding_is_empty_list(self):
        now = datetime.now(timezone.utc)
        node = MemoryNode(
            memory_id="m2",
            created_at=now,
            last_retrieved_at=now,
            agent_role="X",
            summary="S",
            importance_score=0.5,
            retrieval_count=0,
        )
        assert node.embedding == []

    def test_to_dict_serialization(self, node):
        d = node.to_dict()
        assert isinstance(d, dict)
        assert d["memory_id"] == "m1"
        assert d["agent_role"] == "Planner"
        assert d["importance_score"] == 0.85
        assert d["retrieval_count"] == 3
        assert d["embedding"] == [0.1, 0.2, 0.3]

    def test_to_dict_contains_all_fields(self, node):
        d = node.to_dict()
        expected_keys = {
            "memory_id",
            "created_at",
            "last_retrieved_at",
            "agent_role",
            "summary",
            "importance_score",
            "retrieval_count",
            "embedding",
        }
        assert set(d.keys()) == expected_keys

    def test_to_dict_timestamps_present(self, node):
        d = node.to_dict()
        assert isinstance(d["created_at"], datetime)
        assert isinstance(d["last_retrieved_at"], datetime)


# ─────────────────────────────────────────────────────────────────────
# Enumerations
# ─────────────────────────────────────────────────────────────────────


class TestSagaStatus:
    """Validate SagaStatus enum members and string behaviour."""

    def test_all_members_exist(self):
        expected = {
            "PENDING",
            "RUNNING",
            "AWAITING_APPROVAL",
            "COMMITTED",
            "COMPENSATING",
            "ROLLED_BACK",
            "FAILED",
            "COMPENSATION_FAILED",
        }
        actual = {s.value for s in SagaStatus}
        assert actual == expected

    def test_string_comparison(self):
        assert SagaStatus.RUNNING == "RUNNING"
        assert SagaStatus.COMMITTED == "COMMITTED"

    def test_member_count(self):
        assert len(SagaStatus) == 8


class TestStepStatus:
    """Validate StepStatus enum members and string behaviour."""

    def test_all_members_exist(self):
        expected = {
            "PENDING",
            "RUNNING",
            "AWAITING_APPROVAL",
            "COMMITTED",
            "FAILED",
            "COMPENSATING",
            "ROLLED_BACK",
            "COMPENSATION_FAILED",
        }
        actual = {s.value for s in StepStatus}
        assert actual == expected

    def test_string_comparison(self):
        assert StepStatus.PENDING == "PENDING"
        assert StepStatus.FAILED == "FAILED"

    def test_member_count(self):
        assert len(StepStatus) == 8


# ─────────────────────────────────────────────────────────────────────
# SandboxResult
# ─────────────────────────────────────────────────────────────────────


class TestSandboxResult:
    """Validate SandboxResult construction and defaults."""

    def test_construction(self):
        r = SandboxResult(success=True)
        assert r.success is True
        assert r.status == "SUCCESS"
        assert r.data == {}
        assert r.error is None

    def test_failure_result(self):
        r = SandboxResult(success=False, status="FAILED", error="timeout")
        assert r.success is False
        assert r.error == "timeout"


# ─────────────────────────────────────────────────────────────────────
# SagaTransaction
# ─────────────────────────────────────────────────────────────────────


class TestSagaTransaction:
    """Validate SagaTransaction envelope model."""

    def test_defaults(self):
        tx = SagaTransaction(saga_id="tx-1", tenant_id="t1", goal="deploy")
        assert tx.status == SagaStatus.RUNNING.value
        assert tx.start_time == 0.0
        assert tx.completed_steps == []
