"""
SagaMind Security Primitives
============================

Reusable, framework-agnostic security helpers:

* :func:`contain_path` — robust filesystem-jail containment that resolves symlinks
  and ``..`` traversal (the *real* enforcement layer; the Z3 verifier only models a
  string-prefix abstraction of this property).
* :class:`SlidingRateLimiter` — a small in-process fixed-window rate limiter.
* :func:`api_key_auth` — a FastAPI dependency that enforces API-key authentication
  when keys are configured, and is a no-op otherwise (development convenience).

These helpers have no FastAPI import cost unless :func:`api_key_auth` is used, so the
module is safe to import from non-web contexts.
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict

from src.config import settings


class PathSecurityError(Exception):
    """Raised when a path escapes the configured workspace jail."""


def contain_path(path: str, root: str | None = None) -> str:
    """Resolve *path* and guarantee it stays inside *root*.

    Unlike a naive ``str.startswith`` check, this normalises the path, resolves
    symbolic links and ``..`` segments, and verifies true containment via
    :func:`os.path.commonpath`. This defends against:

    * ``/root/../../etc/passwd`` style traversal,
    * symlinks pointing outside the jail,
    * prefix-collision attacks (``/root_evil`` vs ``/root``).

    Args:
        path: Candidate filesystem path (absolute or relative to *root*).
        root: Jail root. Defaults to ``settings.allowed_workspace_root``.

    Returns:
        The fully-resolved, jail-relative-safe absolute path.

    Raises:
        PathSecurityError: If the resolved path is not contained by *root*.
    """
    root = root or settings.allowed_workspace_root
    real_root = os.path.realpath(root)

    candidate = path if os.path.isabs(path) else os.path.join(real_root, path)
    real_path = os.path.realpath(candidate)

    try:
        common = os.path.commonpath([real_root, real_path])
    except ValueError as exc:  # e.g. different drives on Windows
        raise PathSecurityError(
            f"Path '{path}' is not comparable to workspace root '{root}'."
        ) from exc

    if common != real_root:
        raise PathSecurityError(
            f"Path traversal blocked: '{path}' resolves to '{real_path}', "
            f"outside authorized workspace '{real_root}'."
        )
    return real_path


class SlidingRateLimiter:
    """Thread-safe fixed-window rate limiter keyed by an arbitrary identifier."""

    def __init__(self, max_per_minute: int):
        self.max_per_minute = max_per_minute
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """Return True if *key* is under its limit, recording the hit if so."""
        if self.max_per_minute <= 0:
            return True
        now = time.monotonic()
        window_start = now - 60.0
        with self._lock:
            hits = [t for t in self._hits[key] if t >= window_start]
            if len(hits) >= self.max_per_minute:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True


# Singleton limiter sized from configuration.
rate_limiter = SlidingRateLimiter(settings.rate_limit_per_minute)
