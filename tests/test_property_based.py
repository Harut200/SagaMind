"""
Property-based tests using Hypothesis.

These tests verify invariants that unit tests cannot enumerate:
  - Saga FSM: any valid input sequence leaves the saga in a consistent terminal state.
  - Decay math: retention is always in [0.0, 1.0] for any numeric inputs.
  - Security: contain_path either returns an absolute path inside the root or raises.

Hypothesis is an optional dev dependency; the test module is skipped when absent so the
CI unit matrix continues to work without it.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

try:
    from hypothesis import given, settings as h_settings, strategies as st

    HYPOTHESIS_AVAILABLE = True
except ImportError:
    HYPOTHESIS_AVAILABLE = False

pytestmark = pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")

# ── Helpers ──────────────────────────────────────────────────────────────────

if HYPOTHESIS_AVAILABLE:
    from src.models import ActionPayload, SagaStatus, SagaStep, StepStatus
    from src.orchestrator.coordinator import SagaTransactionCoordinator

    _TERMINAL_STATUSES = {
        SagaStatus.COMMITTED.value,
        SagaStatus.ROLLED_BACK.value,
        SagaStatus.COMPENSATION_FAILED.value,
    }

    def _make_step(name: str, fail: bool, comp_fail: bool) -> SagaStep:
        return SagaStep(
            step_id=str(uuid.uuid4()),
            step_name=name,
            action=ActionPayload("NOOP", {}),
            compensation=ActionPayload("NOOP", {}),
            invariants="",
        )

    def _make_coordinator(
        steps_fail: list[bool], comp_fails: list[bool]
    ) -> SagaTransactionCoordinator:
        """Build a coordinator whose sandbox fails forward/compensation steps per the booleans."""
        verifier = MagicMock()
        verifier.verify.return_value = (True, "")

        sandbox = MagicMock()
        execute_calls: list[int] = [0]
        comp_calls: list[int] = [0]

        def _execute(action: Any) -> Any:
            idx = execute_calls[0]
            execute_calls[0] += 1
            if idx < len(steps_fail) and steps_fail[idx]:
                raise RuntimeError("injected step failure")
            from src.models import SandboxResult
            return SandboxResult(success=True)

        def _comp(action: Any) -> bool:
            idx = comp_calls[0]
            comp_calls[0] += 1
            if idx < len(comp_fails) and comp_fails[idx]:
                return False
            return True

        sandbox.execute.side_effect = _execute
        sandbox.execute_compensation.side_effect = _comp
        return SagaTransactionCoordinator(verifier, sandbox)


# ── Saga FSM property ─────────────────────────────────────────────────────────


@pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
@given(
    step_count=st.integers(min_value=0, max_value=6),
    fail_mask=st.integers(min_value=0, max_value=63),
    comp_mask=st.integers(min_value=0, max_value=63),
)
@h_settings(max_examples=200, deadline=2000)
def test_saga_always_reaches_terminal_state(
    step_count: int, fail_mask: int, comp_mask: int
) -> None:
    """For any combination of step failures and compensation failures, the saga must
    end in a terminal consistent state — never left RUNNING or COMPENSATING."""
    steps_fail = [(fail_mask >> i) & 1 == 1 for i in range(step_count)]
    comp_fails = [(comp_mask >> i) & 1 == 1 for i in range(step_count)]

    coord = _make_coordinator(steps_fail, comp_fails)
    saga_id = str(uuid.uuid4())
    coord.start_transaction_log(saga_id, "property-test goal", "test-tenant")

    steps = [_make_step(f"step-{i}", steps_fail[i] if i < len(steps_fail) else False, False)
             for i in range(step_count)]

    try:
        coord.execute_saga(saga_id, steps)
    except Exception:  # noqa: BLE001 - unexpected exceptions are a test failure
        pytest.fail("execute_saga raised an unexpected exception")

    saga = coord.active_sagas.get(saga_id)
    if saga is None:
        # Evicted (max_active_sagas exceeded) — acceptable for large inputs.
        return

    assert saga.status in _TERMINAL_STATUSES, (
        f"Saga ended in non-terminal status '{saga.status}' "
        f"(step_count={step_count}, fail_mask={fail_mask:#06b}, comp_mask={comp_mask:#06b})"
    )


@pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
@given(steps=st.lists(st.booleans(), min_size=1, max_size=8))
@h_settings(max_examples=100, deadline=2000)
def test_completed_steps_in_committed_saga_all_committed(steps: list[bool]) -> None:
    """When a saga commits fully, every step's status is COMMITTED."""
    coord = _make_coordinator([False] * len(steps), [])
    saga_id = str(uuid.uuid4())
    coord.start_transaction_log(saga_id, "all-commit", "t1")
    saga_steps = [_make_step(f"s{i}", False, False) for i in range(len(steps))]
    coord.execute_saga(saga_id, saga_steps)
    saga = coord.active_sagas.get(saga_id)
    if saga and saga.status == SagaStatus.COMMITTED.value:
        for s in saga.completed_steps:
            assert s.status == StepStatus.COMMITTED.value


# ── Decay math property ───────────────────────────────────────────────────────


@pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
@given(
    s_init=st.floats(min_value=0.001, max_value=1000.0, allow_nan=False, allow_infinity=False),
    importance=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    n_access=st.integers(min_value=0, max_value=100_000),
    elapsed_seconds=st.floats(min_value=0.0, max_value=3.156e9, allow_nan=False, allow_infinity=False),
)
@h_settings(max_examples=500, deadline=1000)
def test_retention_always_in_unit_interval(
    s_init: float, importance: float, n_access: int, elapsed_seconds: float
) -> None:
    """Retention must always be in [0.0, 1.0] for any numeric inputs."""
    import math
    from datetime import datetime, timedelta, timezone

    from src.memory.decay import EbbinghausMemoryManager
    from src.models import MemoryNode

    manager = EbbinghausMemoryManager(s_init=s_init)
    now = datetime.now(timezone.utc)
    last_retrieved = now - timedelta(seconds=elapsed_seconds)
    node = MemoryNode(
        memory_id=str(uuid.uuid4()),
        created_at=last_retrieved,
        last_retrieved_at=last_retrieved,
        agent_role="tester",
        summary="property test",
        importance_score=importance,
        retrieval_count=n_access,
    )
    retention = manager.calculate_retention(node)
    assert isinstance(retention, float), f"retention must be float, got {type(retention)}"
    assert not math.isnan(retention), "retention must not be NaN"
    assert 0.0 <= retention <= 1.0, f"retention {retention} out of [0, 1] range"


@pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
@given(
    texts=st.lists(
        st.text(min_size=1, max_size=200, alphabet=st.characters(blacklist_categories=("Cs",))),
        min_size=1,
        max_size=20,
    )
)
@h_settings(max_examples=100, deadline=2000)
def test_deterministic_embeddings_unit_norm(texts: list[str]) -> None:
    """Deterministic embeddings must be unit vectors (norm ≈ 1.0) for any input string."""
    import math

    from src.memory.embedding import _deterministic_embed

    for text in texts:
        vec = _deterministic_embed(text, dim=64)  # small dim for speed
        assert len(vec) == 64
        norm = math.sqrt(sum(v * v for v in vec))
        assert abs(norm - 1.0) < 1e-6, f"embedding for {text!r:.40} has norm {norm}, expected 1.0"


# ── Security property ─────────────────────────────────────────────────────────


@pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
@given(
    segments=st.lists(
        st.text(
            min_size=1,
            max_size=20,
            alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="_-."),
        ),
        min_size=1,
        max_size=8,
    )
)
@h_settings(max_examples=200, deadline=1000)
def test_contain_path_result_always_inside_root(segments: list[str]) -> None:
    """contain_path, when it succeeds, must return a path inside the workspace root."""
    import tempfile

    from src.security import PathSecurityError, contain_path

    with tempfile.TemporaryDirectory() as root:
        candidate = os.path.join(root, *segments)
        try:
            result = contain_path(candidate, root=root)
            # Must be absolute and inside root.
            assert os.path.isabs(result)
            assert os.path.commonpath([root, result]) == root, (
                f"contain_path returned {result!r} which escapes {root!r}"
            )
        except PathSecurityError:
            pass  # Rejection is always acceptable.
