import math
from datetime import datetime, timezone
from typing import Any


class EbbinghausMemoryManager:
    """
    Manages episodic memory retention values.
    Uses decay formulas to flag stale memories.
    """
    def __init__(self, s_init: float = 12.0, tau: float = 0.15, gamma: float = 0.45):
        self.s_init = s_init  # Base half-life in hours
        self.tau = tau        # Retainment threshold
        self.gamma = gamma    # Reinforcement scaling factor

    def calculate_retention(self, memory: Any) -> float:
        """
        Computes retention probability: R(t) = e^(-t / S_m)
        S_m = S_init * (1 + gamma * ln(N_access + 1)) * Importance
        """
        now = datetime.now(timezone.utc)

        # Parse timestamp safely
        last_ret = memory.last_retrieved_at if hasattr(memory, 'last_retrieved_at') else memory.get('last_retrieved_at')
        if isinstance(last_ret, str):
            last_ret = datetime.fromisoformat(last_ret.replace("Z", "+00:00"))

        time_delta_hours = (now - last_ret).total_seconds() / 3600.0

        # Fetch retrieval parameters
        n_access = memory.retrieval_count if hasattr(memory, 'retrieval_count') else memory.get('retrieval_count', 0)
        importance = memory.importance_score if hasattr(memory, 'importance_score') else memory.get('importance_score', 0.5)

        retrieval_bonus = math.log1p(n_access)
        strength = self.s_init * (1.0 + self.gamma * retrieval_bonus) * importance

        if strength <= 0:
            return 0.0

        retention = math.exp(-time_delta_hours / strength)
        return retention

    def evaluate_memories(self, memories: list[Any]) -> tuple[list[Any], list[Any]]:
        """
        Partitions memories into keep (active) and prune (evicted) lists.
        """
        keep_list = []
        prune_list = []

        for m in memories:
            r = self.calculate_retention(m)
            if r >= self.tau:
                keep_list.append(m)
            else:
                prune_list.append(m)

        return keep_list, prune_list
