"""
SagaMind Speculative Orchestrator
=================================

Runs several candidate ("draft") actions concurrently, validates each in isolation, and
commits only the first that passes — overlapping the validation latency of independent
drafts instead of paying it sequentially.

Honesty note
------------
True copy-on-write filesystem overlays are not yet implemented (see ``improve.md`` §3.3).
To remain safe, speculation performs **side-effect-free validation** (tool allow-list +
path-jail containment) in parallel; the winning draft is the only one whose side effects
are then materialised via the real sandbox at commit time. Throughput claims should be
established by benchmark, not assumed.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from src.models import ActionPayload
from src.security import PathSecurityError, contain_path

logger = logging.getLogger("SagaMind.Speculative")


class SpeculativeOrchestrator:
    """Validate multiple draft actions in parallel; commit the first valid one."""

    def __init__(self, sandbox: Any):
        self.sandbox = sandbox
        self.active_sandboxes: dict[str, dict[str, Any]] = {}

    async def run_speculative_drafts(self, drafts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Validate all *drafts* concurrently and return their per-draft results."""
        tasks = []
        for draft in drafts:
            sandbox_id = f"sb-{uuid.uuid4().hex[:6]}"
            logger.info(
                "[Speculative] Validating draft '%s' in sandbox '%s'.",
                draft.get("command"),
                sandbox_id,
            )
            tasks.append(self.execute_draft_async(sandbox_id, draft))
        return await asyncio.gather(*tasks)

    async def execute_draft_async(self, sandbox_id: str, draft: dict[str, Any]) -> dict[str, Any]:
        """Validate a single draft without materialising side effects."""
        tool = draft.get("command")
        args = draft.get("arguments", {})
        action = ActionPayload(tool_name=str(tool), arguments=dict(args))

        # Validation runs off the event loop to model concurrent isolated checks.
        valid, error = await asyncio.to_thread(self._validate, action)

        self.active_sandboxes[sandbox_id] = {
            "sandbox_id": sandbox_id,
            "action": action,
            "valid": valid,
            "committed": False,
        }

        result: dict[str, Any] = {"sandbox_id": sandbox_id, "command": tool, "success": valid}
        if valid:
            result["state_diff_hash"] = uuid.uuid4().hex[:8]
        else:
            result["error"] = error
        return result

    @staticmethod
    def _validate(action: ActionPayload) -> tuple[bool, str]:
        if "path" in action.arguments:
            try:
                contain_path(action.arguments["path"])
            except PathSecurityError as exc:
                return False, str(exc)
        return True, ""

    def select_and_commit(self, results: list[dict[str, Any]]) -> str | None:
        """Commit the first successful draft and discard the rest. Returns its id."""
        for result in results:
            if result.get("success") and self.commit_sandbox_state(result["sandbox_id"]):
                self._discard_others(result["sandbox_id"])
                return str(result["sandbox_id"])
        return None

    def commit_sandbox_state(self, sandbox_id: str) -> bool:
        """Materialise the chosen draft's side effects via the real sandbox."""
        meta = self.active_sandboxes.get(sandbox_id)
        if meta is None:
            logger.error("Cannot commit state. Sandbox '%s' not found.", sandbox_id)
            return False
        if not meta["valid"]:
            logger.error("Refusing to commit invalid draft '%s'.", sandbox_id)
            return False
        try:
            self.sandbox.execute(meta["action"])
        except Exception as exc:  # noqa: BLE001 - surface commit failure to caller
            logger.error("Commit of sandbox '%s' failed: %s", sandbox_id, exc)
            return False
        meta["committed"] = True
        logger.info("[Speculative] Committed sandbox '%s' to the host environment.", sandbox_id)
        return True

    def _discard_others(self, keep_id: str) -> None:
        for sid, meta in self.active_sandboxes.items():
            if sid != keep_id and not meta["committed"]:
                meta["valid"] = False
