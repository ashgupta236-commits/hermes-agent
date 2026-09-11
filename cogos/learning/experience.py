"""Building and validating experience records (L1).

Feature extraction reads mission state *before* the action. That is enforced structurally:
:func:`extract_features` takes only the pre-action state and computes nothing from verification
results, final scores or outcomes, so a future label cannot leak into a state feature by
accident.

Acceptance is separate from collection. Everything is collected — failures, blocks, timeouts and
censored episodes included, because dropping them biases the dataset towards whatever succeeded.
Only records whose receipts resolve, whose versions match, and whose label has a defined meaning
become eligible for learning.
"""

from __future__ import annotations

from typing import Any, Optional

from cogos.schemas.experience import (
    EXPERIENCE_SCHEMA_VERSION,
    FEATURE_VERSION,
    ExperienceRecord,
    OutcomeLabel,
    RewardBreakdown,
    StateFeatures,
)


def extract_features(state: Any, task_family: str = "general") -> StateFeatures:
    """Interpretable pre-action features. Nothing here may consult an outcome."""
    tasks = list(getattr(state, "tasks", []))
    ready = sum(1 for t in tasks if t.status.value == "ready")
    blocked = sum(1 for t in tasks if t.status.value == "blocked")
    open_deps = sum(len(t.depends_on) for t in tasks if t.status.value in ("pending", "ready"))

    claims = list(getattr(state, "claims", []))
    evidenced = sum(1 for c in claims if c.evidence_for)
    completeness = (evidenced / len(claims)) if claims else 0.0

    budget = getattr(state, "budget", None)
    usage = getattr(state, "usage", None)
    remaining = 1.0
    if budget is not None and usage is not None and budget.max_model_calls:
        remaining = max(0.0, 1.0 - (usage.model_calls / float(budget.max_model_calls)))

    failure_class = ""
    for t in reversed(tasks):
        if t.status.value == "failed" and t.failure_reason:
            failure_class = t.failure_signature or t.failure_reason.split(":")[0][:40]
            break

    return StateFeatures(
        version=FEATURE_VERSION,
        task_family=task_family,
        ready_tasks=ready,
        blocked_tasks=blocked,
        open_dependencies=open_deps,
        evidence_completeness=round(completeness, 4),
        unresolved_contradictions=len([c for c in getattr(state, "contradictions", []) if not c.resolved]),
        open_holds=len(state.open_holds()) if hasattr(state, "open_holds") else 0,
        known_tool_count=int(getattr(state, "resources", {}).get("known_tool_count", 0) or 0),
        recent_failure_class=failure_class,
        remaining_budget_fraction=round(remaining, 4),
        cycles_used=int(getattr(usage, "cycles", 0) or 0),
    )


class ExperienceBuilder:
    """Assembles one record per decision, opened before the action and closed after it."""

    def __init__(self, mission_id: str, trajectory_id: str = "", environment_version: str = "", model_identity: str = ""):
        self.mission_id = mission_id
        self.trajectory_id = trajectory_id or mission_id
        self.environment_version = environment_version
        self.model_identity = model_identity
        self._step = 0

    def open(
        self,
        state: Any,
        *,
        task_family: str,
        candidate_actions: list[str],
        chosen_action: str,
        policy_id: str = "",
        policy_version: int = 0,
        behavior_probability: Optional[float] = None,
        permission_scope: Optional[list[str]] = None,
        decision_id: Optional[str] = None,
    ) -> ExperienceRecord:
        rec = ExperienceRecord(
            mission_id=self.mission_id,
            trajectory_id=self.trajectory_id,
            step_index=self._step,
            task_family=task_family,
            decision_id=decision_id,
            state_features=extract_features(state, task_family),
            candidate_actions=list(candidate_actions),
            chosen_action=chosen_action,
            behavior_probability=behavior_probability,
            policy_id=policy_id,
            policy_version=policy_version,
            model_identity=self.model_identity,
            environment_version=self.environment_version,
            permission_scope=list(permission_scope or []),
        )
        self._step += 1
        return rec

    @staticmethod
    def close(
        rec: ExperienceRecord,
        state: Any,
        *,
        label: OutcomeLabel,
        receipts: Optional[list[str]] = None,
        observation_refs: Optional[list[str]] = None,
        artifact_refs: Optional[list[str]] = None,
        cost_usd: float = 0.0,
        elapsed_seconds: float = 0.0,
        terminal: bool = False,
        truncated: bool = False,
    ) -> ExperienceRecord:
        rec.label = label
        rec.outcome_receipts = list(receipts or [])
        rec.observation_refs = list(observation_refs or [])
        rec.artifact_refs = list(artifact_refs or [])
        rec.cost_usd = float(cost_usd)
        rec.elapsed_seconds = float(elapsed_seconds)
        rec.terminal = bool(terminal)
        rec.truncated = bool(truncated)
        rec.next_state_features = None if (terminal or truncated) else extract_features(state, rec.task_family)
        rec.reward = reward_for(label, cost_usd, elapsed_seconds)
        return rec


def reward_for(label: OutcomeLabel, cost_usd: float, elapsed_seconds: float, *, cost_scale: float = 1.0, time_scale: float = 60.0) -> RewardBreakdown:
    """Quality from resolved receipts, minus explicitly scaled resource costs.

    A blocked action is neutral on quality, not punished: refusing to proceed past an
    authorization is correct behaviour and must not be trained out. A timeout is a failure of
    the attempt, not a success, and is scored as such.
    """
    quality = {
        OutcomeLabel.VERIFIED_SUCCESS: 1.0,
        OutcomeLabel.VERIFIED_FAILURE: -1.0,
        OutcomeLabel.BLOCKED: 0.0,
        OutcomeLabel.TIMEOUT: -0.5,
    }.get(label, 0.0)
    return RewardBreakdown(
        verified_quality=quality,
        cost_penalty=-min(1.0, float(cost_usd) / cost_scale) if cost_scale else 0.0,
        time_penalty=-min(1.0, float(elapsed_seconds) / time_scale) if time_scale else 0.0,
    )


def accept_for_learning(
    rec: ExperienceRecord,
    *,
    resolve_receipt: Any,
    environment_version: str = "",
    feature_version: str = FEATURE_VERSION,
) -> tuple[bool, str]:
    """Is this record eligible for a policy update? Returns (accepted, reason).

    Rejection is never deletion: the record stays, quarantined with its reason, so a receipt
    invalidated later can be traced through lineage and the affected policies re-evaluated.
    """
    if rec.schema_version != EXPERIENCE_SCHEMA_VERSION:
        return False, f"unsupported experience schema version {rec.schema_version}"
    if rec.state_features.version != feature_version:
        return False, f"feature version mismatch: record {rec.state_features.version}, learner {feature_version}"
    if environment_version and rec.environment_version and rec.environment_version != environment_version:
        return False, f"environment version mismatch: record {rec.environment_version}, learner {environment_version}"
    if rec.label is OutcomeLabel.UNKNOWN:
        return False, "outcome unknown: no resolvable receipt — this is not a failure, it is unlabelled"
    if rec.label is OutcomeLabel.CENSORED:
        return False, "episode censored before a terminal state: the return was never observed"
    if rec.label in (OutcomeLabel.VERIFIED_SUCCESS, OutcomeLabel.VERIFIED_FAILURE):
        if not rec.outcome_receipts:
            return False, "labelled from verification but carries no receipt"
        for rid in rec.outcome_receipts:
            if resolve_receipt(rid) is None:
                return False, f"receipt {rid} does not resolve"
    if not rec.chosen_action or rec.chosen_action not in rec.candidate_actions:
        return False, "chosen action was not among the candidates recorded at decision time"
    return True, "accepted"


def quarantine(rec: ExperienceRecord, reason: str) -> ExperienceRecord:
    rec.quarantined = True
    rec.quarantine_reason = reason[:300]
    return rec


def affected_by_receipt(records: list[ExperienceRecord], receipt_id: str) -> list[ExperienceRecord]:
    """Lineage lookup: which records rest on a receipt that has just been invalidated."""
    return [r for r in records if receipt_id in r.outcome_receipts]
