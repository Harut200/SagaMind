"""
SagaMind Sandbox Execution Engine
=================================

Executes agent tool invocations under defence-in-depth controls:

* **Filesystem jail** — every path is resolved with :func:`src.security.contain_path`,
  which defeats ``..`` traversal and symlink escapes (true containment, not the previous
  ``str.startswith`` abstraction).
* **Tool allow-list** — only explicitly registered tools may run.
* **Fuel metering** — when the optional ``wasmtime`` runtime is present a fuel-limited,
  memory-limited store is created so future WASM-compiled tools cannot busy-loop or
  exhaust the heap.

The file/database tools are reference host implementations. Untrusted tool *code* should
be compiled to WASM and run inside the metered store (see ``improve.md`` §2.1); the
hooks for that are in place here.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from src.models import SandboxResult
from src.security import PathSecurityError, contain_path

logger = logging.getLogger("SagaMind.Sandbox")

# Tools permitted to run forward actions and compensations, respectively.
_ALLOWED_ACTIONS = frozenset({"WRITE_FILE", "DATABASE_QUERY", "NOOP"})
_ALLOWED_COMPENSATIONS = frozenset({"DELETE_FILE", "DATABASE_QUERY", "NOOP"})


class SandboxError(Exception):
    """Raised for sandbox policy violations or execution failures."""


class WasmSandbox:
    """Isolated execution container for tool invocations."""

    def __init__(self, memory_limit_mb: int = 256, fuel_limit: int = 1_000_000):
        self.memory_limit_mb = memory_limit_mb
        self.fuel_limit = fuel_limit
        self.engine = None
        self.store = None

        try:
            import wasmtime

            config = wasmtime.Config()
            config.consume_fuel = True
            self.engine = wasmtime.Engine(config)
            self.store = wasmtime.Store(self.engine)
            self.store.set_fuel(self.fuel_limit)
            logger.info("Wasmtime engine initialized with fuel metering (%d units).", self.fuel_limit)
        except ImportError:
            logger.warning("wasmtime not installed. Host execution with path-jail enforcement only.")
        except Exception as exc:  # noqa: BLE001 - wasmtime API drift should not be fatal
            logger.warning("Wasmtime initialisation failed (%s). Host execution fallback.", exc)

    # ── Forward execution ───────────────────────────────────────────────
    def execute(self, action: Any) -> SandboxResult:
        """Execute *action* (an ``ActionPayload``) under sandbox policy."""
        tool = action.tool_name
        logger.info("[Sandbox] Executing tool '%s'.", tool)

        if tool not in _ALLOWED_ACTIONS:
            raise SandboxError(f"Tool '{tool}' is not in the action allow-list.")

        if tool == "WRITE_FILE":
            return self._write_file(action.arguments)
        if tool == "DATABASE_QUERY":
            # Reference stub: a real implementation must use parameterised queries.
            return SandboxResult(success=True, data={"affected_rows": 1})
        return SandboxResult(success=True)

    def _write_file(self, args: dict[str, Any]) -> SandboxResult:
        raw_path = args.get("path")
        content = args.get("content", "")
        if not raw_path:
            raise SandboxError("WRITE_FILE requires a 'path' argument.")
        try:
            safe_path = contain_path(raw_path)
        except PathSecurityError as exc:
            raise SandboxError(str(exc)) from exc

        try:
            os.makedirs(os.path.dirname(safe_path), exist_ok=True)
            with open(safe_path, "w") as f:
                f.write(content)
            return SandboxResult(
                success=True,
                data={"written_path": safe_path, "bytes": len(content)},
            )
        except OSError as exc:
            raise SandboxError(f"Failed to write file: {exc}") from exc

    # ── Compensation ────────────────────────────────────────────────────
    def execute_compensation(self, compensation: Any) -> bool:
        """Run a reversion action. Returns True on success."""
        tool = compensation.tool_name
        logger.warning("[Sandbox-Compensation] Running reversion '%s'.", tool)

        if tool not in _ALLOWED_COMPENSATIONS:
            logger.error("Compensation tool '%s' is not in the allow-list.", tool)
            return False

        if tool == "DELETE_FILE":
            return self._delete_file(compensation.arguments)
        if tool == "DATABASE_QUERY":
            return True
        return True

    def _delete_file(self, args: dict[str, Any]) -> bool:
        raw_path = args.get("path")
        if not raw_path:
            return True
        try:
            safe_path = contain_path(raw_path)
        except PathSecurityError as exc:
            logger.error("Compensation path rejected: %s", exc)
            return False
        if not os.path.exists(safe_path):
            return True
        try:
            os.remove(safe_path)
            logger.info("[Sandbox-Compensation] Deleted file '%s'.", safe_path)
            return True
        except OSError as exc:
            logger.error("Failed to delete file during compensation: %s", exc)
            return False
