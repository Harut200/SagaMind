"""
SagaMind Memory Consolidation ("Sleep Cycle")
=============================================

Groups semantically similar episodic memories and distils each group into concept
relationships written to the semantic graph — a computational analogue of hippocampal
replay during sleep.

Clustering is single-linkage connected-components over cosine distance. The pairwise
distances are computed with a single vectorised NumPy matrix product (O(n^2) memory but
BLAS-fast and far cheaper than the previous pure-Python double loop); the grouping
semantics are unchanged so behaviour is deterministic and reproducible.

If an LLM client is supplied, each cluster is labelled with a distilled concept summary;
otherwise a deterministic ``Cluster {n} Concept`` label is used so the pipeline is fully
functional offline and in tests.
"""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger("SagaMind.Memory.Consolidation")


def _embedding(ep: Any) -> Any:
    return ep.get("embedding") if isinstance(ep, dict) else ep.embedding


def _field(ep: Any, name: str) -> Any:
    return ep.get(name) if isinstance(ep, dict) else getattr(ep, name)


class MemoryConsolidator:
    """Clusters episodic memories and projects them into the semantic graph."""

    def __init__(self, timescale_store: Any, neo4j_store: Any, llm_client: Any | None = None):
        self.db = timescale_store
        self.graph = neo4j_store
        self.llm: Any = llm_client

    # ── Distance ────────────────────────────────────────────────────────
    def compute_cosine_distance(self, u: list[float], v: list[float]) -> float:
        """Cosine distance in [0, 2]; a zero vector yields the maximal in-set distance 1.0."""
        dot = sum(a * b for a, b in zip(u, v, strict=False))
        norm_u = math.sqrt(sum(a * a for a in u))
        norm_v = math.sqrt(sum(b * b for b in v))
        if norm_u == 0 or norm_v == 0:
            return 1.0
        return 1.0 - (dot / (norm_u * norm_v))

    def _distance_matrix(self, embeddings: list[list[float]]) -> Any:
        """Return an (n, n) cosine-distance matrix, vectorised when NumPy is present."""
        n = len(embeddings)
        try:
            import numpy as np

            widths = {len(e) for e in embeddings}
            if len(widths) != 1:  # ragged embeddings → fall back to scalar path
                raise ValueError("ragged embeddings")
            m = np.asarray(embeddings, dtype=float)
            norms = np.linalg.norm(m, axis=1)
            safe = norms.copy()
            safe[safe == 0] = 1.0
            unit = m / safe[:, None]
            sim = unit @ unit.T
            dist = 1.0 - sim
            zero = norms == 0
            if zero.any():  # any zero-norm vector is maximally distant from everything
                dist[zero, :] = 1.0
                dist[:, zero] = 1.0
            return dist
        except Exception:  # noqa: BLE001 - NumPy missing or ragged input
            return [[self.compute_cosine_distance(embeddings[i], embeddings[j]) for j in range(n)] for i in range(n)]

    # ── Cluster labelling ───────────────────────────────────────────────
    def _label_cluster(self, cluster_id: int, cluster: list[Any]) -> str:
        """Distil a cluster into a concept label (LLM-backed when available)."""
        if self.llm is None:
            return f"Cluster {cluster_id} Concept"
        summaries = [str(_field(item, "summary")) for item in cluster]
        try:
            return self._llm_summarize(summaries)
        except Exception as exc:  # noqa: BLE001 - never let the LLM break consolidation
            logger.warning("LLM cluster distillation failed, using fallback label: %s", exc)
            return f"Cluster {cluster_id} Concept"

    def _llm_summarize(self, summaries: list[str]) -> str:
        """Override-point / thin wrapper around the configured LLM client."""
        joined = "\n- ".join(summaries)
        prompt = f"Summarise the shared concept behind these agent experiences in a short noun phrase:\n- {joined}"
        return self.llm.summarize(prompt)  # type: ignore[no-any-return]

    # ── Main cycle ──────────────────────────────────────────────────────
    def run_consolidation_cycle(self, tenant_id: str, eps: float = 0.2) -> int:
        """Cluster a tenant's episodic memories and write concept edges.

        Returns the number of clusters (including singletons) discovered.
        """
        logger.info("[Sleep Cycle] Starting consolidation cycle for tenant: '%s'", tenant_id)
        episodes = self._fetch_episodes(tenant_id)

        if len(episodes) < 2:
            logger.info("Insufficient memories to run sleep consolidation cycle.")
            return 0

        clusters = self._cluster(episodes, eps)

        for cid, cluster in clusters.items():
            if len(cluster) < 2:
                continue  # isolated occurrence treated as noise
            concept = self._label_cluster(cid, cluster)
            for item in cluster:
                self.graph.upsert_relationship(
                    source=concept,
                    relation="SUMMARIZES_EXPERIENCE",
                    target=_field(item, "summary"),
                    weight=0.7,
                )
                self.graph.upsert_relationship(
                    source=_field(item, "agent_role"),
                    relation="DISCOVERED_CONCEPT",
                    target=concept,
                    weight=0.5,
                )

        logger.info("Sleep consolidation cycle complete. %d clusters processed.", len(clusters))
        return len(clusters)

    # ── Internals ───────────────────────────────────────────────────────
    def _fetch_episodes(self, tenant_id: str) -> list[Any]:
        if hasattr(self.db, "get_all_memories"):
            episodes: list[Any] = self.db.get_all_memories(tenant_id)
            return episodes
        if hasattr(self.db, "fallback_storage"):
            return [m for m in self.db.fallback_storage if m["tenant_id"] == tenant_id]
        return []

    def _cluster(self, episodes: list[Any], eps: float) -> dict[int, list[Any]]:
        """Single-linkage connected-components grouping over the distance matrix."""
        embeddings = [_embedding(ep) for ep in episodes]
        dist = self._distance_matrix(embeddings)
        n = len(episodes)

        clusters: dict[int, list[Any]] = {}
        assigned: set[int] = set()
        for i in range(n):
            if i in assigned:
                continue
            members = [episodes[i]]
            assigned.add(i)
            for j in range(n):
                if j in assigned:
                    continue
                if dist[i][j] <= eps:
                    members.append(episodes[j])
                    assigned.add(j)
            clusters[len(clusters)] = members
        return clusters
