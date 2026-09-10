"""Calibration tracking: compares predicted confidence with observed outcomes.

Everything here is deterministic. With fewer than :data:`MIN_SAMPLES`
observations the tracker refuses to draw conclusions and leaves confidences
and strategy multipliers untouched.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from cogos.persistence.store import StateStore

MIN_SAMPLES = 10
INSUFFICIENT_NOTE = "insufficient data for calibration"


class CalibrationBin(BaseModel):
    lo: float
    hi: float
    n: int = 0
    mean_predicted: float = 0.0
    observed_rate: float = 0.0


class CalibrationReport(BaseModel):
    domain: Optional[str] = None
    n: int = 0
    brier_score: float = 0.0
    expected_calibration_error: float = 0.0
    bins: list[CalibrationBin] = Field(default_factory=list)
    overconfident: bool = False
    note: str = ""


class CalibrationTracker:
    def __init__(self, store: StateStore, min_samples: int = MIN_SAMPLES):
        self.store = store
        self.min_samples = min_samples

    # -- reporting ----------------------------------------------------------------

    def report(self, domain: Optional[str] = None, bins: int = 5) -> CalibrationReport:
        samples = self.store.calibration_samples(domain)
        n = len(samples)
        bins = max(1, int(bins))
        edges = [i / bins for i in range(bins + 1)]
        buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
        for p, o in samples:
            idx = min(int(p * bins), bins - 1)
            buckets[idx].append((p, o))
        report_bins: list[CalibrationBin] = []
        ece = 0.0
        for i, bucket in enumerate(buckets):
            cb = CalibrationBin(lo=edges[i], hi=edges[i + 1], n=len(bucket))
            if bucket:
                cb.mean_predicted = sum(p for p, _ in bucket) / len(bucket)
                cb.observed_rate = sum(1 for _, o in bucket if o) / len(bucket)
                ece += (len(bucket) / n) * abs(cb.mean_predicted - cb.observed_rate)
            report_bins.append(cb)
        brier = sum((p - (1.0 if o else 0.0)) ** 2 for p, o in samples) / n if n else 0.0
        rep = CalibrationReport(
            domain=domain,
            n=n,
            brier_score=round(brier, 4),
            expected_calibration_error=round(ece, 4),
            bins=report_bins,
        )
        if n < self.min_samples:
            rep.note = f"{INSUFFICIENT_NOTE} (n={n}, need {self.min_samples})"
            return rep
        mean_pred = sum(p for p, _ in samples) / n
        obs = sum(1 for _, o in samples if o) / n
        rep.overconfident = mean_pred - obs > 0.05
        if rep.overconfident:
            rep.note = f"overconfident: mean predicted {mean_pred:.2f} vs observed {obs:.2f}"
        elif obs - mean_pred > 0.05:
            rep.note = f"underconfident: mean predicted {mean_pred:.2f} vs observed {obs:.2f}"
        else:
            rep.note = f"well calibrated: mean predicted {mean_pred:.2f} vs observed {obs:.2f}"
        return rep

    # -- adjustment ---------------------------------------------------------------

    def adjusted_confidence(self, domain: str, raw: float, bins: int = 5) -> float:
        raw = min(1.0, max(0.0, float(raw)))
        rep = self.report(domain, bins=bins)
        if rep.n < self.min_samples:
            return raw
        idx = min(int(raw * bins), bins - 1)
        b = rep.bins[idx]
        if b.n == 0:
            # Fall back to the nearest populated bin.
            populated = [x for x in rep.bins if x.n > 0]
            if not populated:
                return raw
            b = min(populated, key=lambda x: abs(x.mean_predicted - raw))
        adjusted = raw + 0.5 * (b.observed_rate - raw)
        return round(min(1.0, max(0.0, adjusted)), 4)

    def recommendations(self, domain: Optional[str] = None) -> dict[str, Any]:
        rep = self.report(domain)
        base = {
            "verification_depth": 1.0,
            "specialist_spawn_threshold": 1.0,
            "investigation_depth": 1.0,
            "escalation_threshold": 1.0,
            "n": rep.n,
            "note": rep.note,
        }
        if rep.n < self.min_samples:
            return base
        ece = rep.expected_calibration_error
        scale = 1.0 + min(1.0, ece * 2.0)  # 0.0 ECE -> 1.0, 0.5 ECE -> 2.0
        if rep.overconfident:
            base["verification_depth"] = round(scale, 3)
            base["investigation_depth"] = round(scale, 3)
            # Spawn specialists sooner and escalate sooner: lower thresholds.
            base["specialist_spawn_threshold"] = round(1.0 / scale, 3)
            base["escalation_threshold"] = round(1.0 / scale, 3)
        elif "underconfident" in rep.note:
            relax = 1.0 - min(0.3, ece)
            base["verification_depth"] = round(relax, 3)
            base["investigation_depth"] = round(relax, 3)
            base["specialist_spawn_threshold"] = round(1.0 / relax, 3)
            base["escalation_threshold"] = round(1.0 / relax, 3)
        return base
