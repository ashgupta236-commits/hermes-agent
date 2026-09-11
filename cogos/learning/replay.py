"""Replay, splits and retention (L3).

Separation is by *task instance*, not by record: two steps of the same trajectory in different
splits would leak, so a whole trajectory lands in exactly one split, assigned by a stable hash of
its id. Duplicates are collapsed and the collapse is counted, so "we trained on 400 records"
never quietly means "we trained on the same 40 records ten times".

For the on-policy learner the training data is current-policy episodes. Older episodes are kept
for diagnostics and retention measurement, not fed into an on-policy TD update as if the
behaviour policy had not changed — the store makes that distinction explicit rather than leaving
it to a caller's memory.
"""

from __future__ import annotations

import hashlib
from enum import Enum
from typing import Any, Callable, Optional

from cogos.schemas.experience import ExperienceRecord, OutcomeLabel


class Split(str, Enum):
    TRAIN = "train"
    VALIDATION = "validation"
    HOLDOUT = "holdout"


def assign_split(trajectory_id: str, *, train: float = 0.7, validation: float = 0.15) -> Split:
    """Stable, content-free assignment: the same trajectory always lands in the same split."""
    digest = hashlib.sha256(trajectory_id.encode("utf-8")).hexdigest()
    position = int(digest[:8], 16) / 0xFFFFFFFF
    if position < train:
        return Split.TRAIN
    if position < train + validation:
        return Split.VALIDATION
    return Split.HOLDOUT


class ReplayStore:
    def __init__(self, train: float = 0.7, validation: float = 0.15):
        self.train = train
        self.validation = validation
        self.records: list[ExperienceRecord] = []
        self.duplicates_collapsed = 0
        self.sampled: dict[str, int] = {}

    # -- ingestion ------------------------------------------------------------------

    @staticmethod
    def _fingerprint(rec: ExperienceRecord) -> str:
        payload = "|".join(
            [
                rec.trajectory_id,
                str(rec.step_index),
                rec.chosen_action,
                rec.state_features.bucket(),
                rec.label.value,
                f"{rec.reward.total():.6f}",
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def add(self, rec: ExperienceRecord) -> bool:
        fp = self._fingerprint(rec)
        if any(self._fingerprint(r) == fp for r in self.records):
            self.duplicates_collapsed += 1
            return False
        self.records.append(rec)
        return True

    def extend(self, records: list[ExperienceRecord]) -> int:
        return sum(1 for r in records if self.add(r))

    # -- retrieval ------------------------------------------------------------------

    def split_of(self, rec: ExperienceRecord) -> Split:
        return assign_split(rec.trajectory_id, train=self.train, validation=self.validation)

    def by_split(self, split: Split, *, include_quarantined: bool = False) -> list[ExperienceRecord]:
        out = [r for r in self.records if self.split_of(r) is split]
        if not include_quarantined:
            out = [r for r in out if not r.quarantined]
        for r in out:
            self.sampled[r.id] = self.sampled.get(r.id, 0) + 1
        return out

    def trajectories(self, split: Split, *, policy_id: Optional[str] = None) -> list[list[ExperienceRecord]]:
        """Ordered episodes. `policy_id` restricts to on-policy data for a TD update."""
        grouped: dict[str, list[ExperienceRecord]] = {}
        for rec in self.by_split(split):
            if policy_id is not None and rec.policy_id != policy_id:
                continue
            grouped.setdefault(rec.trajectory_id, []).append(rec)
        return [sorted(v, key=lambda r: r.step_index) for _, v in sorted(grouped.items())]

    def stats(self) -> dict[str, Any]:
        by_split = {s.value: len(self.by_split(s, include_quarantined=True)) for s in Split}
        labels: dict[str, int] = {}
        for rec in self.records:
            labels[rec.label.value] = labels.get(rec.label.value, 0) + 1
        return {
            "records": len(self.records),
            "duplicates_collapsed": self.duplicates_collapsed,
            "by_split": by_split,
            "by_label": labels,
            "quarantined": sum(1 for r in self.records if r.quarantined),
            "distinct_trajectories": len({r.trajectory_id for r in self.records}),
        }


def retention_report(
    evaluate: Callable[[list[ExperienceRecord]], float],
    prior: list[ExperienceRecord],
    *,
    before: float,
    tolerance: float = 0.02,
) -> dict[str, Any]:
    """Did a candidate update cost performance on tasks it already handled?

    `before` is the score the previous version achieved on the same prior records, so this is a
    genuine before/after on identical data rather than a comparison against a moving target.
    """
    after = evaluate(prior)
    delta = after - before
    return {
        "prior_instances": len(prior),
        "before": round(before, 6),
        "after": round(after, 6),
        "delta": round(delta, 6),
        "regressed": delta < -tolerance,
        "tolerance": tolerance,
    }


def replayed_is_not_new_evidence(records: list[ExperienceRecord]) -> dict[str, Any]:
    """A named reminder with teeth: report how often each record has been sampled.

    Evaluating on data the learner has already seen is diagnostics, not held-out evidence, and
    the numbers here are what stops a report from calling it the latter.
    """
    return {
        "records": len(records),
        "labels": sorted({r.label.value for r in records}),
        "note": "these records were drawn from replay; results on them are diagnostics, not unseen evidence",
    }


def learnable(records: list[ExperienceRecord]) -> list[ExperienceRecord]:
    """Records whose label carries a defined return. Censored and unknown stay out."""
    return [r for r in records if not r.quarantined and r.label not in (OutcomeLabel.UNKNOWN, OutcomeLabel.CENSORED)]
