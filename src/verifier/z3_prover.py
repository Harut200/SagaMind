"""
SagaMind Z3 Formal Logic Verifier
=================================

Neuro-symbolic safety gate. Each proposed action carries an SMT-LIB2 invariant; the
verifier proves that the action's concrete arguments cannot violate it before execution.

Method (the academically correct refutation procedure)
------------------------------------------------------
1. Declare an SMT constant for every action argument, typed from its Python value.
2. Constrain each constant to its concrete value.
3. Parse the caller-supplied invariant (arbitrary SMT-LIB2) against those declarations.
4. Assert the **negation** of the invariant and ``check-sat``:
   * ``sat``   → a model violates the invariant → reject with the counter-example,
   * ``unsat`` → the invariant is entailed by the arguments → accept,
   * ``unknown``/timeout → **fail closed** (reject).

When the ``z3-solver`` package is absent the verifier degrades to a conservative semantic
guard that enforces only the workspace path-prefix property (string-prefix abstraction;
true path containment is enforced separately in the sandbox).
"""

from __future__ import annotations

import logging
from typing import Any

from src.config import settings

logger = logging.getLogger("SagaMind.Verifier.Z3")


class Z3Verifier:
    """Formal logic prover using the Z3 SMT solver Python bindings."""

    def __init__(self) -> None:
        self.z3_active = False
        try:
            import z3  # noqa: F401

            self.z3_active = True
            logger.info("Z3 Solver Python bindings successfully loaded.")
        except ImportError:
            logger.warning("z3-solver package not installed. Running semantic validation fallback.")

    def verify(self, action_args: dict[str, Any], invariants_string: str) -> tuple[bool, str]:
        """Prove that *action_args* satisfy *invariants_string*.

        Returns ``(is_safe, explanation)``.
        """
        if not self.z3_active:
            return self._fallback_verify(action_args)
        return self._z3_verify(action_args, invariants_string)

    # ── Z3 path ─────────────────────────────────────────────────────────
    def _z3_verify(self, action_args: dict[str, Any], invariants_string: str) -> tuple[bool, str]:
        import z3

        solver = z3.Solver()
        solver.set("timeout", settings.z3_timeout_ms)

        decls: dict[str, Any] = {}
        for key, val in action_args.items():
            var = self._declare(z3, key, val)
            if var is None:
                continue
            decls[key] = var
            solver.add(var == self._literal(z3, val))

        invariant = self._parse_invariant(z3, invariants_string, decls)
        if invariant is None:
            # No (parseable) invariant supplied: nothing to refute → accept.
            return True, "No invariant supplied; action admitted."

        # Refutation: look for an assignment that satisfies the args but breaks the invariant.
        solver.add(z3.Not(invariant))
        result = solver.check()

        if result == z3.sat:
            model = solver.model()
            counter_example = {str(d): str(model[d]) for d in model.decls()}
            logger.error("Safety verification failed. Counter-example: %s", counter_example)
            return False, f"Safety constraint violation. Counter-example state: {counter_example}"
        if result == z3.unsat:
            logger.info("Invariants proved. Action is formally safe.")
            return True, "Verification successful."
        # unknown / timeout → fail closed.
        logger.warning("Z3 returned 'unknown' (timeout=%dms). Failing closed.", settings.z3_timeout_ms)
        return False, "SMT solver could not resolve the invariant within the timeout; rejected."

    @staticmethod
    def _declare(z3: Any, key: str, val: Any) -> Any:
        if isinstance(val, bool):
            return z3.Bool(key)
        if isinstance(val, str):
            return z3.String(key)
        if isinstance(val, (int, float)):
            return z3.Real(key)
        return None

    @staticmethod
    def _literal(z3: Any, val: Any) -> Any:
        if isinstance(val, bool):
            return z3.BoolVal(val)
        if isinstance(val, str):
            return z3.StringVal(val)
        return z3.RealVal(val)

    @staticmethod
    def _parse_invariant(z3: Any, invariants_string: str, decls: dict[str, Any]) -> Any:
        """Parse arbitrary SMT-LIB2 assertions, binding declared argument constants."""
        if not invariants_string or not invariants_string.strip():
            return None
        try:
            assertions = z3.parse_smt2_string(invariants_string, decls=decls)
            if len(assertions) == 0:
                return None
            return z3.And(*assertions) if len(assertions) > 1 else assertions[0]
        except z3.Z3Exception as exc:
            logger.error("Failed to parse SMT-LIB2 invariant: %s", exc)
            # Unparseable invariant must not silently pass → treat as unsatisfiable guard.
            return z3.BoolVal(False)

    # ── Fallback path (z3 unavailable) ──────────────────────────────────
    def _fallback_verify(self, action_args: dict[str, Any]) -> tuple[bool, str]:
        """Conservative semantic guard: workspace path-prefix check only."""
        if "path" in action_args:
            path = action_args["path"]
            allowed_root = settings.allowed_workspace_root
            if not path.startswith(allowed_root):
                return False, (
                    f"Semantic Guard Fail: Path '{path}' accesses files "
                    f"outside authorized workspace '{allowed_root}'."
                )
        return True, "Mock Validation Success (Solver bypassed)"
