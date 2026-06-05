"""
SagaMind — Speculative Orchestrator Tests
=========================================

Validates parallel side-effect-free draft validation and winner-commit semantics.
"""

import pytest

from src.orchestrator.sandbox import WasmSandbox
from src.speculative.orchestrator import SpeculativeOrchestrator


@pytest.fixture
def jailed(tmp_path, monkeypatch):
    from src import config, security

    monkeypatch.setattr(config.settings, "allowed_workspace_root", str(tmp_path))
    monkeypatch.setattr(security.settings, "allowed_workspace_root", str(tmp_path))
    return SpeculativeOrchestrator(WasmSandbox()), tmp_path


class TestSpeculativeValidation:
    async def test_valid_and_invalid_drafts_partitioned(self, jailed):
        orch, root = jailed
        drafts = [
            {"command": "WRITE_FILE", "arguments": {"path": str(root / "ok.txt"), "content": "x"}},
            {"command": "WRITE_FILE", "arguments": {"path": "/etc/passwd", "content": "x"}},
        ]
        results = await orch.run_speculative_drafts(drafts)
        assert results[0]["success"] is True
        assert results[1]["success"] is False
        assert "error" in results[1]

    async def test_validation_has_no_side_effects(self, jailed):
        orch, root = jailed
        target = root / "draft.txt"
        await orch.run_speculative_drafts(
            [{"command": "WRITE_FILE", "arguments": {"path": str(target), "content": "x"}}]
        )
        # Validation must not write anything until commit.
        assert not target.exists()


class TestSelectAndCommit:
    async def test_commit_first_valid_draft_materialises_side_effect(self, jailed):
        orch, root = jailed
        target = root / "committed.txt"
        results = await orch.run_speculative_drafts(
            [{"command": "WRITE_FILE", "arguments": {"path": str(target), "content": "done"}}]
        )
        committed = orch.select_and_commit(results)
        assert committed is not None
        assert target.read_text() == "done"

    async def test_no_valid_draft_commits_nothing(self, jailed):
        orch, _ = jailed
        results = await orch.run_speculative_drafts(
            [{"command": "WRITE_FILE", "arguments": {"path": "/root/x", "content": "x"}}]
        )
        assert orch.select_and_commit(results) is None

    def test_commit_unknown_sandbox_is_false(self, jailed):
        orch, _ = jailed
        assert orch.commit_sandbox_state("sb-missing") is False
