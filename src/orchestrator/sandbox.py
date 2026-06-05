"""
SagaMind Sandbox Execution Engine
=================================

Executes agent tool invocations under defence-in-depth controls:

* **Filesystem jail** — every path is resolved with :func:`src.security.contain_path`,
  defeating ``..`` traversal and symlink escapes.
* **Pluggable ToolRegistry** — tools are registered as ``ToolDefinition`` objects that
  carry forward-action handler, compensation handler, and an optional WASM module path.
  The hardcoded ``if/elif`` dispatch is gone; adding a new tool is a one-liner registry
  call.
* **Fuel metering** — when ``wasmtime`` is present a fuel-limited store is created so
  future WASM-compiled tools cannot busy-loop.

The built-in reference tools (``WRITE_FILE``, ``DELETE_FILE``, ``DATABASE_QUERY``,
``NOOP``) are registered at module import time. Untrusted tool *code* should be compiled
to WASM and registered with a ``wasm_module_path`` so it runs inside ``run_wasm_module``
(see ``improve.md`` §6.1 / §2.1).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from src.config import settings
from src.models import SandboxResult
from src.security import PathSecurityError, contain_path

logger = logging.getLogger("SagaMind.Sandbox")


class SandboxError(Exception):
    """Raised for sandbox policy violations or execution failures."""


# ── Tool registry ────────────────────────────────────────────────────


@dataclass
class ToolDefinition:
    """Describes a registered tool: its forward handler, compensation, and optional WASM path."""

    name: str
    handler: Callable[[dict[str, Any]], SandboxResult]
    compensation_handler: Callable[[dict[str, Any]], bool] | None = None
    wasm_module_path: str | None = None
    description: str = ""
    allowed_as_compensation: bool = False


class ToolRegistry:
    """Central registry of all permitted tools. Replaces the hardcoded frozenset."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._comp_tools: set[str] = set()

    def register(self, tool: ToolDefinition, *, allow_as_compensation: bool = False) -> None:
        self._tools[tool.name] = tool
        if allow_as_compensation:
            self._comp_tools.add(tool.name)

    def get(self, name: str) -> ToolDefinition:
        if name not in self._tools:
            raise SandboxError(f"Tool '{name}' is not registered. Allowed: {sorted(self._tools)}")
        return self._tools[name]

    def is_compensation_allowed(self, name: str) -> bool:
        return name in self._comp_tools

    @property
    def allowed_actions(self) -> frozenset[str]:
        return frozenset(self._tools)

    @property
    def allowed_compensations(self) -> frozenset[str]:
        return frozenset(self._comp_tools)


# Module-level singleton — extended by calling ``registry.register(...)`` at startup.
registry = ToolRegistry()


# ── Reference tool implementations ──────────────────────────────────


def _handle_write_file(args: dict[str, Any]) -> SandboxResult:
    raw_path = args.get("path")
    content = args.get("content", "")
    if not raw_path:
        raise SandboxError("WRITE_FILE requires a 'path' argument.")
    try:
        safe_path = contain_path(raw_path)
    except PathSecurityError as exc:
        raise SandboxError(str(exc)) from exc
    try:
        os.makedirs(os.path.dirname(safe_path) or ".", exist_ok=True)
        with open(safe_path, "w") as f:
            f.write(content)
        return SandboxResult(success=True, data={"written_path": safe_path, "bytes": len(content)})
    except OSError as exc:
        raise SandboxError(f"Failed to write file: {exc}") from exc


def _handle_delete_file(args: dict[str, Any]) -> SandboxResult:
    raw_path = args.get("path")
    if not raw_path:
        return SandboxResult(success=True)
    try:
        safe_path = contain_path(raw_path)
    except PathSecurityError as exc:
        raise SandboxError(str(exc)) from exc
    if not os.path.exists(safe_path):
        return SandboxResult(success=True)
    try:
        os.remove(safe_path)
        return SandboxResult(success=True, data={"deleted_path": safe_path})
    except OSError as exc:
        raise SandboxError(f"Failed to delete file: {exc}") from exc


def _comp_delete_file(args: dict[str, Any]) -> bool:
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


def _handle_database_query(args: dict[str, Any]) -> SandboxResult:
    # Reference stub — real implementation must use parameterised queries against
    # a named table (never raw SQL). The API layer validates args via DatabaseQueryArgs.
    return SandboxResult(success=True, data={"affected_rows": 1})


def _handle_noop(_args: dict[str, Any]) -> SandboxResult:
    return SandboxResult(success=True)


def _comp_noop(_args: dict[str, Any]) -> bool:
    return True


# Register built-in tools at import time.
registry.register(
    ToolDefinition("WRITE_FILE", _handle_write_file, description="Write content to a jailed path"),
    allow_as_compensation=False,
)
registry.register(
    ToolDefinition(
        "DELETE_FILE",
        _handle_delete_file,
        compensation_handler=_comp_delete_file,
        description="Delete a file (used as compensation for WRITE_FILE)",
        allowed_as_compensation=True,
    ),
    allow_as_compensation=True,
)
registry.register(
    ToolDefinition(
        "DATABASE_QUERY",
        _handle_database_query,
        compensation_handler=lambda _: True,
        description="Execute a parameterised database operation",
        allowed_as_compensation=True,
    ),
    allow_as_compensation=True,
)
registry.register(
    ToolDefinition(
        "NOOP",
        _handle_noop,
        compensation_handler=_comp_noop,
        description="No-op placeholder",
        allowed_as_compensation=True,
    ),
    allow_as_compensation=True,
)


# ── Sandbox engine ───────────────────────────────────────────────────


class WasmSandbox:
    """Isolated execution container delegating to the ToolRegistry."""

    def __init__(self, memory_limit_mb: int = 256, fuel_limit: int = 1_000_000):
        self.memory_limit_mb = memory_limit_mb
        self.fuel_limit = fuel_limit
        self.engine = None

        try:
            import wasmtime

            config = wasmtime.Config()
            config.consume_fuel = True
            self.engine = wasmtime.Engine(config)
            logger.info("Wasmtime engine initialized (fuel=%d).", self.fuel_limit)
        except ImportError:
            logger.warning("wasmtime not installed. Host execution with path-jail enforcement only.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Wasmtime initialisation failed (%s). Host execution fallback.", exc)

    # ── Forward execution ────────────────────────────────────────────
    def execute(self, action: Any) -> SandboxResult:
        """Execute *action* (an ``ActionPayload``) via the ToolRegistry."""
        tool_def = registry.get(action.tool_name)
        logger.info("[Sandbox] Executing tool '%s'.", action.tool_name)

        if tool_def.wasm_module_path:
            with open(tool_def.wasm_module_path, "rb") as f:
                return self.run_wasm_module(f.read())

        return tool_def.handler(action.arguments)

    # ── Compensation ─────────────────────────────────────────────────
    def execute_compensation(self, compensation: Any) -> bool:
        """Run a reversion action via the ToolRegistry. Returns True on success."""
        tool_name = compensation.tool_name
        logger.warning("[Sandbox-Compensation] Running reversion '%s'.", tool_name)

        if not registry.is_compensation_allowed(tool_name):
            logger.error("Compensation tool '%s' is not in the allow-list.", tool_name)
            return False

        tool_def = registry.get(tool_name)
        if tool_def.compensation_handler is None:
            logger.error("Tool '%s' has no compensation handler registered.", tool_name)
            return False

        return tool_def.compensation_handler(compensation.arguments)

    # ── True WASM isolation ──────────────────────────────────────────
    def run_wasm_module(
        self, wasm_bytes: bytes, entrypoint: str = "_start", preopen_root: str | None = None
    ) -> SandboxResult:
        """Execute a WASI module with fuel metering and a workspace-only directory preopen.

        Untrusted compiled tools should be routed here instead of the host handlers above.
        """
        if not self.engine:
            raise SandboxError("WASM runtime unavailable (install the 'wasm' extra: wasmtime).")
        import wasmtime

        root = preopen_root or settings.allowed_workspace_root
        store = wasmtime.Store(self.engine)
        store.set_fuel(self.fuel_limit)

        wasi = wasmtime.WasiConfig()
        wasi.preopen_dir(root, "/")
        store.set_wasi(wasi)

        linker = wasmtime.Linker(self.engine)
        linker.define_wasi()
        try:
            module = wasmtime.Module(self.engine, wasm_bytes)
            instance = linker.instantiate(store, module)
            func = instance.exports(store).get(entrypoint)
            if func is None:
                raise SandboxError(f"WASM module has no export '{entrypoint}'.")
            func(store)
        except wasmtime.WasmtimeError as exc:
            raise SandboxError(f"WASM execution trapped: {exc}") from exc
        return SandboxResult(success=True, data={"entrypoint": entrypoint, "jail": root})
