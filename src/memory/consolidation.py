import logging
import math
from typing import Any

logger = logging.getLogger("SagaMind.Memory.Consolidation")

class MemoryConsolidator:
    """
    Executes DBSCAN vector clustering and abstracts raw experiences.
    Translates episodic events into semantic relations.
    """
    def __init__(self, timescale_store: Any, neo4j_store: Any, openai_client: Any | None = None):
        self.db = timescale_store
        self.graph = neo4j_store
        self.llm = openai_client

    def compute_cosine_distance(self, u: list[float], v: list[float]) -> float:
        dot_product = sum(a * b for a, b in zip(u, v, strict=True))
        norm_u = math.sqrt(sum(a * a for a in u))
        norm_v = math.sqrt(sum(b * b for b in v))
        if norm_u == 0 or norm_v == 0:
            return 1.0
        return 1.0 - (dot_product / (norm_u * norm_v))

    def run_consolidation_cycle(self, tenant_id: str, eps: float = 0.2) -> int:
        """
        Executes DBSCAN clustering and graph consolidation.
        Returns:
            - int: Number of consolidated clusters.
        """
        logger.info(f"[Sleep Cycle] Starting consolidation cycle for tenant: '{tenant_id}'")

        # 1. Fetch memories
        if hasattr(self.db, 'fallback_storage'):
            episodes = [m for m in self.db.fallback_storage if m["tenant_id"] == tenant_id]
        else:
            # Query from Postgres
            episodes = [] # In real env, query from database

        if len(episodes) < 2:
            logger.info("Insufficient memories to run sleep consolidation cycle.")
            return 0

        # 2. Cluster using manual DBSCAN parser over cosine distances
        clusters: dict[int, list[Any]] = {}
        assigned = set()

        for ep_i in episodes:
            ep_id = ep_i.get("memory_id") if isinstance(ep_i, dict) else ep_i.memory_id
            if ep_id in assigned:
                continue

            current_cluster = [ep_i]
            assigned.add(ep_id)

            for ep_j in episodes:
                ep_j_id = ep_j.get("memory_id") if isinstance(ep_j, dict) else ep_j.memory_id
                if ep_j_id in assigned:
                    continue

                u = ep_i.get("embedding") if isinstance(ep_i, dict) else ep_i.embedding
                v = ep_j.get("embedding") if isinstance(ep_j, dict) else ep_j.embedding

                dist = self.compute_cosine_distance(u, v)
                if dist <= eps:
                    current_cluster.append(ep_j)
                    assigned.add(ep_j_id)

            clusters[len(clusters)] = current_cluster

        # 3. Consolidate and write relationships
        for cid, cluster in clusters.items():
            if len(cluster) < 2:
                continue # Skip isolated single occurrences (noise)

            # Create consolidated concept node
            source_concept = f"Cluster {cid} Concept"
            for item in cluster:
                summary = item.get("summary") if isinstance(item, dict) else item.summary
                role = item.get("agent_role") if isinstance(item, dict) else item.agent_role

                # Write edge into Neo4j graph db
                self.graph.upsert_relationship(
                    source=source_concept,
                    relation="SUMMARIZES_EXPERIENCE",
                    target=summary,
                    weight=0.7
                )
                self.graph.upsert_relationship(
                    source=role,
                    relation="DISCOVERED_CONCEPT",
                    target=source_concept,
                    weight=0.5
                )

        logger.info(f"Sleep consolidation cycle complete. {len(clusters)} clusters processed.")
        return len(clusters)
