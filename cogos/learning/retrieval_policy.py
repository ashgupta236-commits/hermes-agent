"""The one concrete decision the learner controls first (L2).

Which memory-retrieval strategy to use before a selection step. It is a deliberately small
choice among three *already-validated*, equivalent-effect options: every one of them reads the
same memory store through the same interface, none of them changes what the runtime is permitted
to do, and the worst outcome of a bad choice is a less useful set of recalled lines.

That is the point. The learner earns influence on a decision where being wrong is cheap and the
feedback is real, and it is structurally incapable of reaching decisions where being wrong is
not — the strategies are a closed set, and :func:`constrained_actions` filters that set before
any learned value is consulted.

Feedback is measured, not asserted: a strategy is rewarded for recalling material the cycle
actually used, penalised for recalling quarantined content, and given nothing for volume.
"""

from __future__ import annotations

from typing import Any, Optional

from cogos.learning.policy import ContextualBandit, constrained_actions
from cogos.schemas.experience import StateFeatures
from cogos.schemas.memory import MemoryClass

POLICY_NAME = "memory_retrieval_strategy"

#: All three are validated paths through the existing memory manager. `relevance` is the
#: baseline: it is what the runtime did before any of this existed.
STRATEGIES: dict[str, dict[str, Any]] = {
    "relevance": {"classes": None, "limit": 6, "query": "objective"},
    "failure_first": {"classes": [MemoryClass.FAILURE, MemoryClass.PROCEDURAL], "limit": 6, "query": "objective"},
    "procedural_first": {"classes": [MemoryClass.PROCEDURAL, MemoryClass.SEMANTIC], "limit": 6, "query": "objective"},
}
BASELINE = "relevance"


class LearnedRetrieval:
    """Chooses a retrieval strategy, then scores what that choice actually produced."""

    def __init__(self, bandit: Optional[ContextualBandit] = None):
        self.bandit = bandit or ContextualBandit(POLICY_NAME, list(STRATEGIES), baseline=BASELINE, exploration=0.3, seed=11)
        self.last: Optional[tuple[str, str]] = None

    def choose(self, features: StateFeatures) -> tuple[str, str]:
        context = features.bucket()
        allowed = constrained_actions(STRATEGIES, authorized=set(STRATEGIES))
        action, why = self.bandit.select(context, allowed)
        self.last = (context, action)
        return action, why

    @staticmethod
    def parameters(strategy: str) -> dict[str, Any]:
        return dict(STRATEGIES.get(strategy) or STRATEGIES[BASELINE])

    def reward(self, *, used: int, retrieved: int, quarantined: int) -> float:
        """Measured usefulness of the retrieval this strategy produced.

        `used` is how many recalled lines survived injection screening and reached the
        workspace. Volume alone earns nothing — the ratio is what is scored — and recalling
        poisoned content is a real cost.
        """
        if retrieved <= 0:
            return 0.0
        return round((used / retrieved) - 0.5 * (quarantined / retrieved), 6)

    def record(self, *, used: int, retrieved: int, quarantined: int) -> Optional[float]:
        """Apply the measured reward to the strategy that was actually used."""
        if self.last is None:
            return None
        context, action = self.last
        value = self.bandit.update(context, action, self.reward(used=used, retrieved=retrieved, quarantined=quarantined))
        self.last = None
        return value
