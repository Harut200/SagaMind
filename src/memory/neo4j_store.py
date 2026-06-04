import logging
from typing import Any

from src.config import settings

logger = logging.getLogger("SagaMind.Memory.Neo4j")

class Neo4jGraphStore:
    """
    Interface manager for the neocortical semantic memory layer.
    Manages conceptual graphs, relationships, and nodes lookup.
    """
    def __init__(self):
        self.driver = None
        self.active = False
        # In-memory adjacency list fallback for graph simulation
        self.fallback_nodes: dict[str, dict[str, Any]] = {}
        self.fallback_relationships: list[dict[str, Any]] = []

        try:
            from neo4j import GraphDatabase
            self.driver = GraphDatabase.driver(
                settings.neo4j_uri,
                auth=(settings.neo4j_user, settings.neo4j_pass)
            )
            self.active = True
            logger.info("Neo4j graph driver successfully initialized.")
        except Exception as e:
            logger.warning(f"Neo4j server connection failed. Running in-memory graph simulator: {str(e)}")

    def upsert_relationship(self, source: str, relation: str, target: str, weight: float = 0.5):
        """
        Inserts or merges nodes and relationships.
        """
        if not self.active:
            # Upsert locally inside our fallback graph variables
            self.fallback_nodes[source] = {"name": source, "type": "Concept"}
            self.fallback_nodes[target] = {"name": target, "type": "Concept"}
            self.fallback_relationships.append({
                "source": source,
                "target": target,
                "type": relation,
                "weight": weight
            })
            logger.info(f"[In-Memory Graph] Upserted: ({source})-[{relation} {{weight: {weight}}}]->({target})")
            return

        with self.driver.session() as session:
            try:
                # Merge entities and set relationship weights
                session.run("""
                    MERGE (s:Concept {name: $source})
                    MERGE (t:Concept {name: $target})
                    MERGE (s)-[r:RELATION {type: $relation}]->(t)
                    ON CREATE SET r.weight = $weight
                    ON MATCH SET r.weight = $weight + (1.0 - r.weight)*0.1;
                """, source=source, target=target, relation=relation, weight=weight)
                logger.info(f"[Neo4j] Upserted relationship: ({source})-[{relation}]->({target})")
            except Exception as e:
                logger.error(f"Failed to upsert relationship to Neo4j graph: {str(e)}")

    def close(self):
        if self.active and self.driver:
            self.driver.close()
