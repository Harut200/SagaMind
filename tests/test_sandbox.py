"""
SagaMind — Sandbox Tests
========================

Validates the filesystem jail, tool allow-list, typed results, and compensation logic.
"""

import pytest

from src.models import ActionPayload, SandboxResult
from src.orchestrator.sandbox import SandboxError, WasmSandbox


@pytest.fixture
def jailed_sandbox(tmp_path, monkeypatch):
    """A sandbox whose workspace jail is an isolated temp directory."""
    from src import config, security

    monkeypatch.setattr(config.settings, "allowed_workspace_root", str(tmp_path))
    monkeypatch.setattr(security.settings, "allowed_workspace_root", str(tmp_path))
    return WasmSandbox(), tmp_path


class TestWriteFile:
    def test_write_inside_jail_succeeds(self, jailed_sandbox):
        sandbox, root = jailed_sandbox
        target = root / "sub" / "out.txt"
        result = sandbox.execute(ActionPayload("WRITE_FILE", {"path": str(target), "content": "hello"}))
        assert isinstance(result, SandboxResult)
        assert result.success is True
        assert target.read_text() == "hello"

    def test_write_outside_jail_rejected(self, jailed_sandbox):
        sandbox, _ = jailed_sandbox
        with pytest.raises(SandboxError):
            sandbox.execute(ActionPayload("WRITE_FILE", {"path": "/etc/passwd", "content": "x"}))

    def test_traversal_rejected(self, jailed_sandbox):
        sandbox, root = jailed_sandbox
        with pytest.raises(SandboxError):
            sandbox.execute(ActionPayload("WRITE_FILE", {"path": str(root / ".." / "x.txt"), "content": "x"}))

    def test_missing_path_rejected(self, jailed_sandbox):
        sandbox, _ = jailed_sandbox
        with pytest.raises(SandboxError):
            sandbox.execute(ActionPayload("WRITE_FILE", {"content": "x"}))


class TestToolAllowList:
    def test_unknown_tool_rejected(self, jailed_sandbox):
        sandbox, _ = jailed_sandbox
        with pytest.raises(SandboxError):
            sandbox.execute(ActionPayload("RM_RF", {"path": "/"}))

    def test_database_query_succeeds(self, jailed_sandbox):
        sandbox, _ = jailed_sandbox
        result = sandbox.execute(ActionPayload("DATABASE_QUERY", {"query": "SELECT 1"}))
        assert result.success is True


class TestCompensation:
    def test_delete_file_inside_jail(self, jailed_sandbox):
        sandbox, root = jailed_sandbox
        target = root / "f.txt"
        target.write_text("data")
        ok = sandbox.execute_compensation(ActionPayload("DELETE_FILE", {"path": str(target)}))
        assert ok is True
        assert not target.exists()

    def test_delete_missing_file_is_noop_success(self, jailed_sandbox):
        sandbox, root = jailed_sandbox
        ok = sandbox.execute_compensation(ActionPayload("DELETE_FILE", {"path": str(root / "nope.txt")}))
        assert ok is True

    def test_delete_outside_jail_rejected(self, jailed_sandbox):
        sandbox, _ = jailed_sandbox
        ok = sandbox.execute_compensation(ActionPayload("DELETE_FILE", {"path": "/etc/hosts"}))
        assert ok is False

    def test_unknown_compensation_rejected(self, jailed_sandbox):
        sandbox, _ = jailed_sandbox
        assert sandbox.execute_compensation(ActionPayload("WIPE_DISK", {})) is False
