"""Learned values and action selection (L2).

Two learners, both with real parameters that really move and really change the next eligible
decision:

* :class:`ContextualBandit` — one concrete decision among already-validated, equivalent-effect
  options, with a fixed baseline, logged feedback and an explicit data-support check.
* :class:`LinearSarsaQ` — a bounded on-policy action-value learner for a task family with
  multi-step credit assignment, using linear function approximation:

  .. math:: \\delta_t = r_t + \\gamma m_t Q_\\theta(s_{t+1}, a_{t+1}) - Q_\\theta(s_t, a_t)
  .. math:: \\theta \\leftarrow \\theta + \\alpha \\delta_t \\nabla_\\theta Q_\\theta(s_t, a_t)

  with ``m_t = 0`` at a genuine terminal state and ``1`` for a continuing transition. This is
  on-policy: the target uses the action actually taken next. Truncation is treated explicitly —
  a truncated episode has no observed continuation, so its target is refused rather than
  fabricated by bootstrapping from a state that was never reached under the policy.

Both are bounded by the same rule: **the learner narrows a choice, it never widens authority.**
:func:`constrained_actions` filters candidates down to what is already authorized and unheld
before any value is consulted, so a high-value action that is denied, held, or gated on evidence
is simply not on the menu. The learner cannot select a prohibited tool, weaken a verifier,
suppress a hold, or increase its own permissions.
"""

from __future__ import annotations

import math
import random
from typing import Any, Callable, Iterable, Optional

from cogos.schemas.experience import ExperienceRecord, PolicyVersion, StateFeatures

#: Below this many observations for a (context, action) pair, the learned estimate is not
#: trusted and the validated baseline is used instead.
MIN_SUPPORT = 3


def constrained_actions(
    candidates: Iterable[str],
    *,
    authorized: Optional[set[str]] = None,
    held: Optional[set[str]] = None,
    evidence_gated: Optional[set[str]] = None,
) -> list[str]:
    """Hard constraints applied *before* any value is consulted.

    Authorization, an active hold and an evidence gate are not costs to be traded against
    expected return; they remove the option. Filtering here rather than penalising in the reward
    is what makes "the learner cannot buy its way past a gate" a property of the code.
    """
    out = []
    for action in candidates:
        if authorized is not None and action not in authorized:
            continue
        if held and action in held:
            continue
        if evidence_gated and action in evidence_gated:
            continue
        out.append(action)
    return out


def dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class ContextualBandit:
    """Per-(context, action) mean reward with an uncertainty bonus and a baseline fallback.

    Sufficient statistics — counts and running means — are the parameters, and they persist. The
    baseline is a fixed, already-validated action: outside supported contexts the bandit returns
    it rather than exploiting an estimate it has no data for.
    """

    algorithm = "contextual_bandit/ucb1-tuned-lite"

    def __init__(self, name: str, actions: list[str], baseline: str, *, exploration: float = 0.5, seed: int = 0, min_support: int = MIN_SUPPORT):
        if baseline not in actions:
            raise ValueError("the baseline must be one of the actions")
        self.name = name
        self.actions = list(actions)
        self.baseline = baseline
        self.exploration = float(exploration)
        self.min_support = int(min_support)
        self.rng = random.Random(seed)
        self.seed = seed
        self.counts: dict[str, int] = {}
        self.means: dict[str, float] = {}
        self.update_count = 0

    @staticmethod
    def key(context: str, action: str) -> str:
        return f"{context}||{action}"

    def support(self, context: str, action: str) -> int:
        return self.counts.get(self.key(context, action), 0)

    def value(self, context: str, action: str) -> float:
        return self.means.get(self.key(context, action), 0.0)

    def supported(self, context: str, action: str) -> bool:
        return self.support(context, action) >= self.min_support

    def select(self, context: str, candidates: list[str]) -> tuple[str, str]:
        """Return (action, why). The baseline is chosen whenever data support is absent."""
        options = [a for a in candidates if a in self.actions]
        if not options:
            return self.baseline, "no learned action is available here; using the validated baseline"
        supported = [a for a in options if self.supported(context, a)]
        if not supported:
            fallback = self.baseline if self.baseline in options else options[0]
            return fallback, f"context '{context}' has fewer than {self.min_support} observations per action; using the validated baseline"
        total = sum(self.support(context, a) for a in supported)
        best, best_score = supported[0], -math.inf
        for action in supported:
            n = self.support(context, action)
            bonus = self.exploration * math.sqrt(math.log(max(2, total)) / n)
            score = self.value(context, action) + bonus
            if score > best_score:
                best, best_score = action, score
        return best, f"learned estimate {self.value(context, best):+.3f} over {self.support(context, best)} observation(s)"

    def update(self, context: str, action: str, reward: float) -> float:
        """Incremental mean. Returns the new estimate, so a caller can assert it moved."""
        key = self.key(context, action)
        n = self.counts.get(key, 0) + 1
        mean = self.means.get(key, 0.0)
        mean += (float(reward) - mean) / n
        self.counts[key] = n
        self.means[key] = mean
        self.update_count += 1
        return mean

    def learn_from(self, records: list[ExperienceRecord]) -> int:
        applied = 0
        for rec in records:
            if rec.quarantined or rec.chosen_action not in self.actions:
                continue
            self.update(rec.state_features.bucket(), rec.chosen_action, rec.reward.total())
            applied += 1
        return applied

    def to_version(self, version: int = 0, manifest: Optional[list[str]] = None) -> PolicyVersion:
        pv = PolicyVersion(
            name=self.name,
            algorithm=self.algorithm,
            version=version,
            learning_rate=0.0,
            discount=0.0,
            initialization="zeros",
            seed=self.seed,
            update_count=self.update_count,
            parameters={"means": [self.means[k] for k in sorted(self.means)]},
            statistics={"keys": sorted(self.means), "counts": {k: self.counts[k] for k in sorted(self.counts)}, "baseline": self.baseline, "min_support": self.min_support},
            training_manifest=list(manifest or []),
        )
        pv.seal_manifest()
        return pv

    def load_version(self, pv: PolicyVersion) -> None:
        keys = list(pv.statistics.get("keys") or [])
        values = list(pv.parameters.get("means") or [])
        self.means = {k: float(v) for k, v in zip(keys, values)}
        self.counts = {k: int(v) for k, v in (pv.statistics.get("counts") or {}).items()}
        self.update_count = int(pv.update_count)


class CensoredTarget(RuntimeError):
    """The transition has no observed continuation, so no TD target exists for it."""


class LinearSarsaQ:
    """On-policy linear action-value learner. Parameters are per-action weight vectors."""

    algorithm = "sarsa/linear"

    def __init__(self, name: str, actions: list[str], *, alpha: float = 0.1, gamma: float = 0.9, seed: int = 0, dimension: int = 0):
        self.name = name
        self.actions = list(actions)
        self.alpha = float(alpha)
        self.gamma = float(gamma)
        self.seed = seed
        self.dimension = dimension or StateFeatures.dimension()
        self.theta: dict[str, list[float]] = {a: [0.0] * self.dimension for a in self.actions}
        self.update_count = 0

    def q(self, features: StateFeatures, action: str) -> float:
        weights = self.theta.get(action)
        if weights is None:
            return 0.0
        return dot(features.vector(), weights)

    def td_error(self, rec: ExperienceRecord, next_action: Optional[str]) -> float:
        """δ_t = r_t + γ·m_t·Q(s_{t+1}, a_{t+1}) − Q(s_t, a_t).

        Raises :class:`CensoredTarget` when the episode was truncated rather than terminated:
        bootstrapping there would invent a continuation that was never observed.
        """
        current = self.q(rec.state_features, rec.chosen_action)
        reward = rec.reward.total()
        if rec.terminal:
            return reward - current  # m_t = 0
        if rec.truncated or rec.next_state_features is None:
            raise CensoredTarget(f"experience {rec.id} was truncated: no valid continuing next state to bootstrap from")
        if next_action is None:
            raise CensoredTarget(f"experience {rec.id} has a next state but no sampled next action: an on-policy target needs a_(t+1)")
        return reward + self.gamma * rec.mask() * self.q(rec.next_state_features, next_action) - current

    def update(self, rec: ExperienceRecord, next_action: Optional[str]) -> float:
        """One SARSA step. Returns δ_t so a caller can check the parameters moved by α·δ·∇Q."""
        delta = self.td_error(rec, next_action)
        grad = rec.state_features.vector()  # ∇θ Q = φ(s,a) for a linear Q
        weights = self.theta.setdefault(rec.chosen_action, [0.0] * self.dimension)
        for i, g in enumerate(grad):
            weights[i] += self.alpha * delta * g
        self.update_count += 1
        return delta

    def learn_from_trajectory(self, trajectory: list[ExperienceRecord]) -> dict[str, Any]:
        """Train on one on-policy episode, in order. Censored transitions are skipped and counted."""
        applied = 0
        censored = 0
        deltas: list[float] = []
        for i, rec in enumerate(trajectory):
            if rec.quarantined:
                continue
            next_action = trajectory[i + 1].chosen_action if i + 1 < len(trajectory) else None
            try:
                deltas.append(self.update(rec, next_action))
                applied += 1
            except CensoredTarget:
                censored += 1
        return {"applied": applied, "censored": censored, "deltas": deltas}

    def select(self, features: StateFeatures, candidates: list[str], *, support: Optional[Callable[[str], int]] = None, min_support: int = MIN_SUPPORT, baseline: Optional[str] = None) -> tuple[str, str]:
        options = [a for a in candidates if a in self.theta]
        if not options:
            return (baseline or ""), "no learned action is available here"
        if support is not None:
            options = [a for a in options if support(a) >= min_support] or []
            if not options:
                fallback = baseline or candidates[0]
                return fallback, f"insufficient data support; using the validated baseline '{fallback}'"
        best = max(options, key=lambda a: self.q(features, a))
        return best, f"Q={self.q(features, best):+.4f}"

    def to_version(self, version: int = 0, manifest: Optional[list[str]] = None) -> PolicyVersion:
        pv = PolicyVersion(
            name=self.name,
            algorithm=self.algorithm,
            feature_version=StateFeatures().version,
            version=version,
            learning_rate=self.alpha,
            discount=self.gamma,
            initialization="zeros",
            seed=self.seed,
            update_count=self.update_count,
            parameters={a: list(w) for a, w in self.theta.items()},
            statistics={"dimension": self.dimension, "actions": list(self.actions)},
            training_manifest=list(manifest or []),
        )
        pv.seal_manifest()
        return pv

    def load_version(self, pv: PolicyVersion) -> None:
        self.theta = {a: list(map(float, w)) for a, w in pv.parameters.items()}
        self.dimension = int(pv.statistics.get("dimension") or self.dimension)
        self.alpha = float(pv.learning_rate)
        self.gamma = float(pv.discount)
        self.update_count = int(pv.update_count)


class PolicyStore:
    """Versioned policies with a real rollback target.

    A candidate is inert until it is activated, activation records what it rolls back to, and
    the previous version stays on disk — so "roll back" is an operation, not an intention.
    """

    KEY = "learned_policies"

    def __init__(self, store: Any):
        self.store = store

    def _all(self) -> dict[str, Any]:
        return dict(self.store.kv_get(self.KEY, {}) or {})

    def versions(self, name: str) -> list[PolicyVersion]:
        raw = self._all().get(name) or []
        return [PolicyVersion.model_validate(v) for v in raw]

    def save(self, pv: PolicyVersion) -> PolicyVersion:
        data = self._all()
        existing = list(data.get(pv.name) or [])
        pv.version = len(existing) + 1
        pv.seal_manifest()
        existing.append(pv.model_dump(mode="json"))
        data[pv.name] = existing[-20:]
        self.store.kv_set(self.KEY, data)
        return pv

    def active(self, name: str) -> Optional[PolicyVersion]:
        for pv in reversed(self.versions(name)):
            if pv.active:
                return pv
        return None

    def activate(self, name: str, policy_id: str) -> Optional[PolicyVersion]:
        data = self._all()
        entries = list(data.get(name) or [])
        previous = self.active(name)
        activated: Optional[PolicyVersion] = None
        for entry in entries:
            pv = PolicyVersion.model_validate(entry)
            if pv.id == policy_id:
                pv.active = True
                pv.rollback_to = previous.id if previous is not None else None
                activated = pv
            else:
                pv.active = False
            entry.clear()
            entry.update(pv.model_dump(mode="json"))
        data[name] = entries
        self.store.kv_set(self.KEY, data)
        return activated

    def rollback(self, name: str) -> Optional[PolicyVersion]:
        current = self.active(name)
        if current is None or not current.rollback_to:
            return None
        return self.activate(name, current.rollback_to)
