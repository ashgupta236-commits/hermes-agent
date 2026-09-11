"""Experience and policy records for the learning kernel (L1).

Three things get conflated under "the agent learns", and this module keeps them apart:

* **storing experience** — writing what happened (this schema);
* **changing the external agent** — a versioned policy whose parameters actually move and
  actually change the next decision (:mod:`cogos.learning.policy`);
* **training model parameters** — updating the hosted model's weights. That does **not** happen
  here and cannot: the learning layer sits outside a frozen model.

Two rules are enforced rather than documented. Features come only from information available
*before* the action, so a test result, final score or post-hoc explanation can never reach a
state feature. And an outcome is eligible for learning only when its receipts resolve, its
environment and policy versions match, and its label has a defined meaning — with failed,
blocked, timed-out and censored episodes kept rather than dropped, because an unknown outcome
is not a failure and a timeout is not a success.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from cogos.ids import iso_now, new_id

EXPERIENCE_SCHEMA_VERSION = 1
FEATURE_VERSION = "features/1"


class OutcomeLabel(str, Enum):
    """What actually happened. Every value has a defined meaning; none of them is a guess."""

    VERIFIED_SUCCESS = "verified_success"  # receipts resolve and passed
    VERIFIED_FAILURE = "verified_failure"  # receipts resolve and failed
    BLOCKED = "blocked"  # denied, held, or requiring an authorization that did not arrive
    TIMEOUT = "timeout"  # ran out of time; explicitly not a success
    CENSORED = "censored"  # truncated before a terminal state: the return is not observed
    UNKNOWN = "unknown"  # no resolvable receipt; explicitly not a failure


#: Labels that carry a defined return. CENSORED and UNKNOWN are kept but never bootstrapped as
#: if their return were observed.
LEARNABLE_LABELS = frozenset({OutcomeLabel.VERIFIED_SUCCESS, OutcomeLabel.VERIFIED_FAILURE, OutcomeLabel.BLOCKED, OutcomeLabel.TIMEOUT})


class StateFeatures(BaseModel):
    """Interpretable features, all computable strictly before the action is taken.

    No embeddings yet: they go in when their version, privacy boundary and measured value are
    clear, not before.
    """

    version: str = FEATURE_VERSION
    task_family: str = "general"
    ready_tasks: int = 0
    blocked_tasks: int = 0
    open_dependencies: int = 0
    evidence_completeness: float = Field(default=0.0, ge=0.0, le=1.0)
    unresolved_contradictions: int = 0
    open_holds: int = 0
    known_tool_count: int = 0
    recent_failure_class: str = ""
    remaining_budget_fraction: float = Field(default=1.0, ge=0.0, le=1.0)
    cycles_used: int = 0

    def vector(self) -> list[float]:
        """Fixed-order numeric encoding. Order is part of the feature version."""
        return [
            1.0,  # bias
            min(1.0, self.ready_tasks / 10.0),
            min(1.0, self.blocked_tasks / 10.0),
            min(1.0, self.open_dependencies / 10.0),
            self.evidence_completeness,
            min(1.0, self.unresolved_contradictions / 5.0),
            min(1.0, self.open_holds / 3.0),
            min(1.0, self.known_tool_count / 20.0),
            1.0 if self.recent_failure_class else 0.0,
            self.remaining_budget_fraction,
        ]

    @staticmethod
    def dimension() -> int:
        return 10

    def bucket(self) -> str:
        """Coarse discrete context for tabular/bandit lookups and data-support checks."""
        return "|".join(
            [
                self.task_family,
                "ready" if self.ready_tasks else "noready",
                "contra" if self.unresolved_contradictions else "clean",
                "held" if self.open_holds else "free",
                "lowbudget" if self.remaining_budget_fraction < 0.25 else "budget",
                self.recent_failure_class or "nofail",
            ]
        )


class RewardBreakdown(BaseModel):
    """Reward components, kept separate so nobody has to trust a single number.

    Hard constraints live *outside* the reward: an authorization, an active hold and an
    evidence gate are not prices, so a numerically attractive action still cannot buy its way
    past them. Nothing here rewards verbosity, confidence, agent count, self-praise, or a
    model's declaration that it learned.
    """

    verified_quality: float = Field(default=0.0, description="From independently resolved receipts only")
    cost_penalty: float = Field(default=0.0, description="Scaled resource cost, always <= 0")
    time_penalty: float = Field(default=0.0, description="Scaled elapsed time, always <= 0")
    quality_coefficient: float = 1.0
    cost_coefficient: float = 0.2
    time_coefficient: float = 0.1

    def total(self) -> float:
        return round(
            self.quality_coefficient * self.verified_quality
            + self.cost_coefficient * self.cost_penalty
            + self.time_coefficient * self.time_penalty,
            6,
        )


class ExperienceRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("exp"))
    schema_version: int = EXPERIENCE_SCHEMA_VERSION
    mission_id: str = ""
    trajectory_id: str = Field(default="", description="Lineage to the complete task trajectory")
    step_index: int = 0
    task_family: str = "general"
    decision_id: Optional[str] = None

    state_features: StateFeatures = Field(default_factory=StateFeatures)
    candidate_actions: list[str] = Field(default_factory=list, description="What was actually available at decision time")
    chosen_action: str = ""
    behavior_probability: Optional[float] = Field(default=None, description="Defined only when the choice was sampled")

    policy_id: str = ""
    policy_version: int = 0
    model_identity: str = ""
    environment_version: str = ""
    permission_scope: list[str] = Field(default_factory=list)

    observation_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    outcome_receipts: list[str] = Field(default_factory=list, description="Verification ids that must resolve for this record to be eligible")

    label: OutcomeLabel = OutcomeLabel.UNKNOWN
    reward: RewardBreakdown = Field(default_factory=RewardBreakdown)
    cost_usd: float = 0.0
    elapsed_seconds: float = 0.0
    terminal: bool = False
    truncated: bool = False
    next_state_features: Optional[StateFeatures] = None

    quarantined: bool = False
    quarantine_reason: str = ""
    created_at: str = Field(default_factory=iso_now)

    def bootstrappable(self) -> bool:
        """Whether a TD target may bootstrap from the next state.

        Only when a genuine continuing next state exists. A truncated episode has no observed
        continuation, so its target is censored rather than quietly fabricated.
        """
        return not self.terminal and not self.truncated and self.next_state_features is not None

    def mask(self) -> float:
        """`m_t` in the SARSA update: 0 at a genuine terminal state, 1 for a continuing one."""
        return 0.0 if self.terminal else 1.0


class PolicyVersion(BaseModel):
    """Everything needed to reproduce, compare and roll back a candidate policy."""

    id: str = Field(default_factory=lambda: new_id("pol"))
    name: str = ""
    algorithm: str = ""
    feature_version: str = FEATURE_VERSION
    version: int = 0
    learning_rate: float = 0.1
    discount: float = 0.9
    initialization: str = "zeros"
    seed: int = 0
    update_count: int = 0
    parameters: dict[str, list[float]] = Field(default_factory=dict)
    statistics: dict[str, Any] = Field(default_factory=dict)
    training_manifest: list[str] = Field(default_factory=list, description="Experience ids this version was trained on")
    manifest_hash: str = ""
    rollback_to: Optional[str] = None
    active: bool = False
    created_at: str = Field(default_factory=iso_now)

    def seal_manifest(self) -> str:
        self.manifest_hash = hashlib.sha256(json.dumps(sorted(self.training_manifest)).encode("utf-8")).hexdigest()
        return self.manifest_hash
