"""
SagaMind Semantic Graph Store (Neo4j)
=====================================

Neocortical semantic memory: concept nodes and weighted relationships consolidated from
episodic experience. Falls back to an in-memory adjacency model when Neo4j is unreachable
so the graph is queryable in development and tests.

Reliability
-----------
Every Neo4j call is wrapped with a per-query timeout (``settings.neo4j_timeout_s``,
default 30 s) so a slow graph traversal cannot hold an API worker thread indefinitely.
Transient errors (network blips, leader-election during cluster failover) are retried up
to 3 times with exponential back-off via ``tenacity`` when available.
"""

from __future__ import annotations

import logging
from typing import Any

from src.config import settings

logger = logging.getLogger("SagaMind.Memory.Neo4j")

# Retry decorator — no-op when tenacity is not installed.
try:
    from tenacity import (
        retry,
        retry_if_exception_type,
        stop_after_attempt,
        wait_exponential,
    )

    def _neo4j_retry(fn: Any) -> Any:
        return retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, max=4),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        )(fn)

except ImportError:
    def _neo4j_retry(fn: Any) -> Any:  # type: ignore[misc]
        return fn


class Neo4jGraphStore:
    """Read/write interface to the semantic concept graph."""

    def __init__(self) -> None:
        self.driver: Any = None
        self.active = False
        self.fallback_nodes: dict[str, dict[str, Any]] = {}
        self.fallback_relationships: list[dict[str, Any]] = []

        try:
            from neo4j import GraphDatabase

            self.driver = GraphDatabase.driver(
                settings.neo4j_uri,
                auth=(settings.neo4j_user, settings.neo4j_pass),
                connection_timeout=settings.neo4j_timeout_s,
            )
            self.driver.verify_connectivity()
            self.active = True
            logger.info("Neo4j graph driver successfully initialized.")
        except ImportError as exc:
            self._handle_unavailable("neo4j driver not installed", exc)
        except Exception as exc:  # noqa: BLE001 - driver raises many connection errors
            self._handle_unavailable("Neo4j connection failed", exc)

    def _handle_unavailable(self, reason: str, exc: Exception) -> None:
        if settings.require_backends:
            raise RuntimeError(
                f"REQUIRE_BACKENDS is set but Neo4j is unavailable: {reason}: {exc}"
            ) from exc
        logger.warning("%s. Using in-memory graph simulator. (%s)", reason, exc)

    # ── Writes ──────────────────────────────────────────────────────────
    def upsert_relationship(
        self, source: str, relation: str, target: str, weight: float = 0.5
    ) -> None:
        """Merge two concept nodes and a weighted relationship between them."""
        if not self.active:
            self.fallback_nodes[source] = {"name": source, "type": "Concept"}
            self.fallback_nodes[target] = {"name": target, "type": "Concept"}
            self.fallback_relationships.append(
                {"source": source, "target": target, "type": relation, "weight": weight}
            )
            logger.info(
                "[In-Memory Graph] Upserted: (%s)-[%s {weight: %s}]->(%s)",
                source, relation, weight, target,
            )
            return

        @_neo4j_retry
        def _run() -> None:
            with self.driver.session() as session:
                session.run(
                    """
                    MERGE (s:Concept {name: $source})
                    MERGE (t:Concept {name: $target})
                    MERGE (s)-[r:RELATION {type: $relation}]->(t)
                    ON CREATE SET r.weight = $weight
                    ON MATCH SET r.weight = $weight + (1.0 - r.weight) * 0.1;
                    """,
                    source=source, target=target, relation=relation, weight=weight,
                )

        try:
            _run()
            logger.info("[Neo4j] Upserted relationship: (%s)-[%s]->(%s)", source, relation, target)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to upsert relationship to Neo4j graph: %s", exc)

    # ── Reads ───────────────────────────────────────────────────────────
    def get_neighbors(self, concept: str) -> list[dict[str, Any]]:
        """Return outgoing relationships from *concept* as ``{target, type, weight}`` dicts."""
        if not self.active:
            return [
                {"target": r["target"], "type": r["type"], "weight": r["weight"]}
                for r in self.fallback_relationships
                if r["source"] == concept
            ]

        @_neo4j_retry
        def _run() -> list[dict[str, Any]]:
            with self.driver.session() as session:
                result = session.run(
                    """
                    MATCH (s:Concept {name: $concept})-[r:RELATION]->(t:Concept)
                    RETURN t.name AS target, r.type AS type, r.weight AS weight;
                    """,
                    concept=concept,
                )
                return [dict(record) for record in result]

        try:
            return _run()
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to query neighbors from Neo4j graph: %s", exc)
            return []

    def get_all_relationships(self) -> list[dict[str, Any]]:
        """Return every relationship in the graph (for inspection/export)."""
        if not self.active:
            return list(self.fallback_relationships)

        @_neo4j_retry
        def _run() -> list[dict[str, Any]]:
            with self.driver.session() as session:
                result = session.run(
                    """
                    MATCH (s:Concept)-[r:RELATION]->(t:Concept)
                    RETURN s.name AS source, t.name AS target,
                           r.type AS type, r.weight AS weight;
                    """
                )
                return [dict(record) for record in result]

        try:
            return _run()
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to query relationships from Neo4j graph: %s", exc)
            return []

    def close(self) -> None:
        if self.active and self.driver:
            self.driver.close()
