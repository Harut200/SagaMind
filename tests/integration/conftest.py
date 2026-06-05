"""
Integration test gating.

These tests exercise *live* TimescaleDB / Neo4j / Redis instances. They are skipped by
default so the unit suite stays hermetic and offline. Enable them with::

    RUN_INTEGRATION=1 pytest -m integration

CI brings the services up via `docker compose` (or testcontainers) and sets the flag.
"""

import os

import pytest

if not os.getenv("RUN_INTEGRATION"):
    pytest.skip(
        "integration tests disabled (set RUN_INTEGRATION=1 to enable)",
        allow_module_level=True,
    )
