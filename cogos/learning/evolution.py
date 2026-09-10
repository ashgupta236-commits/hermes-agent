"""Bounded skill and code evolution (L6).

The lifecycle is: propose a candidate from validated failures, build it in isolation, execute it,
grade it against *fixed* contracts, compare with the baseline, archive lineage and results,
activate only when eligible, monitor retention, roll back on a declared regression.

The separation that makes it safe is between three authorities:

* **candidate** — may edit its own variant and nothing else;
* **evaluator** — owns the grading contract, the outcome records and the release gates;
* **controller** — owns activation.

A candidate cannot alter the contract it is graded by, the outcomes it produced, the permission
policy, the release gates, or the controller that accepts its change. :class:`ReleaseGate` refuses
a manifest whose evaluation reference does not match the contract that actually ran, so
"redefine success so the candidate looks better" fails as a mechanism rather than as a matter of
policy.

Activation is transactional and every release freezes a manifest — source hashes, dependencies,
model configuration, policy version, evaluation references and a rollback target — so rolling
back is an operation with a destination, not a wish. Producing a patch is not publishing a
release: `scope` starts at `local` and only evidence and authorization move it.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

from cogos.ids import iso_now, new_id


class CandidateStatus(str, Enum):
    PROPOSED = "proposed"
    BUILT = "built"
    EVALUATED = "evaluated"
    ELIGIBLE = "eligible"
    ACTIVE = "active"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"
    SHADOW = "shadow"  # machinery complete, promotion blocked for want of an independent gate


class ReleaseScope(str, Enum):
    LOCAL = "local"
    PROJECT = "project"
    PUBLISHED = "published"


class GradingContract(BaseModel):
    """The fixed external standard. Candidates never write to this."""

    id: str = Field(default_factory=lambda: new_id("gc"))
    name: str
    required_checks: list[str] = Field(default_factory=list)
    minimum_improvement: float = 0.05
    max_retention_regression: float = 0.02
    owner: str = Field(default="evaluator", description="Never 'candidate'")
    independent: bool = Field(default=False, description="True only when the holdout is genuinely inaccessible to candidates")
    version: int = 1

    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(
                {"name": self.name, "checks": sorted(self.required_checks), "min": self.minimum_improvement, "reg": self.max_retention_regression, "v": self.version},
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()


class EvaluationReference(BaseModel):
    """The result of running a candidate against a contract, bound to that contract's fingerprint."""

    id: str = Field(default_factory=lambda: new_id("ev"))
    contract_id: str
    contract_fingerprint: str
    baseline_score: float = 0.0
    candidate_score: float = 0.0
    checks_passed: list[str] = Field(default_factory=list)
    checks_failed: list[str] = Field(default_factory=list)
    retention_delta: float = 0.0
    executed: bool = False
    evidence: list[str] = Field(default_factory=list)
    ran_at: str = Field(default_factory=iso_now)

    def improvement(self) -> float:
        return round(self.candidate_score - self.baseline_score, 6)


class ReleaseManifest(BaseModel):
    """Frozen at release time. Everything needed to reproduce or undo the change."""

    id: str = Field(default_factory=lambda: new_id("rel"))
    candidate_id: str
    name: str
    source_hashes: dict[str, str] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list)
    model_configuration: dict[str, Any] = Field(default_factory=dict)
    policy_version: Optional[str] = None
    evaluation_reference_id: str = ""
    contract_fingerprint: str = ""
    rollback_to: Optional[str] = None
    scope: ReleaseScope = ReleaseScope.LOCAL
    frozen_at: str = Field(default_factory=iso_now)

    def seal(self) -> str:
        payload = self.model_dump(mode="json")
        payload.pop("id", None)
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


class Candidate(BaseModel):
    id: str = Field(default_factory=lambda: new_id("cand"))
    name: str
    origin_failures: list[str] = Field(default_factory=list, description="The validated failures this candidate answers")
    variant_path: str = ""
    status: CandidateStatus = CandidateStatus.PROPOSED
    evaluation_reference_id: Optional[str] = None
    release_id: Optional[str] = None
    reasons: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=iso_now)


class ReleaseGate:
    """Decides eligibility. Owned by the evaluator, never by the candidate."""

    def __init__(self, contract: GradingContract):
        self.contract = contract
        self.fingerprint = contract.fingerprint()

    def eligible(self, evaluation: EvaluationReference) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if evaluation.contract_id != self.contract.id:
            reasons.append("evaluation was graded against a different contract")
        if evaluation.contract_fingerprint != self.fingerprint:
            # The contract changed between grading and release — including a candidate
            # rewriting it to make itself look better. Either way the result is not usable.
            reasons.append("the grading contract changed after this evaluation ran")
        if not evaluation.executed:
            reasons.append("the candidate was never executed: a score with no run behind it is not evidence")
        missing = [c for c in self.contract.required_checks if c not in evaluation.checks_passed]
        if missing:
            reasons.append("required checks not passed: " + ", ".join(missing[:5]))
        if evaluation.checks_failed:
            reasons.append("failed checks: " + ", ".join(evaluation.checks_failed[:5]))
        if evaluation.improvement() < self.contract.minimum_improvement:
            reasons.append(f"improvement {evaluation.improvement():+.4f} below the declared minimum {self.contract.minimum_improvement:+.4f}")
        if evaluation.retention_delta < -self.contract.max_retention_regression:
            reasons.append(f"retention regressed by {evaluation.retention_delta:+.4f}, beyond the tolerated {-self.contract.max_retention_regression:+.4f}")
        return (not reasons), reasons

    def promotion_blocked_reason(self) -> Optional[str]:
        """Why production promotion is withheld even for an eligible candidate.

        When the holdout is visible to candidates it is a developer fixture, not an independent
        benchmark, and saying otherwise would be the relabelling the brief forbids.
        """
        if not self.contract.independent:
            return (
                "the grading contract's holdout is developer-visible, so it is a development fixture rather than an "
                "independent benchmark; the local candidate path is complete but production promotion stays blocked"
            )
        return None


class EvolutionRegistry:
    """Candidates, evaluations and releases, with transactional activation and real rollback."""

    KEY = "evolution_registry"

    def __init__(self, store: Any):
        self.store = store

    def _all(self) -> dict[str, Any]:
        data = dict(self.store.kv_get(self.KEY, {}) or {})
        data.setdefault("candidates", [])
        data.setdefault("evaluations", [])
        data.setdefault("releases", [])
        data.setdefault("active", None)
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.store.kv_set(self.KEY, data)

    # -- lifecycle ------------------------------------------------------------------

    def propose(self, name: str, origin_failures: list[str], variant_path: str = "") -> Candidate:
        cand = Candidate(name=name, origin_failures=list(origin_failures), variant_path=variant_path, status=CandidateStatus.BUILT if variant_path else CandidateStatus.PROPOSED)
        data = self._all()
        data["candidates"].append(cand.model_dump(mode="json"))
        self._write(data)
        return cand

    def record_evaluation(self, cand: Candidate, evaluation: EvaluationReference) -> EvaluationReference:
        data = self._all()
        data["evaluations"].append(evaluation.model_dump(mode="json"))
        cand.status = CandidateStatus.EVALUATED
        cand.evaluation_reference_id = evaluation.id
        self._update_candidate(data, cand)
        self._write(data)
        return evaluation

    def decide(self, cand: Candidate, evaluation: EvaluationReference, gate: ReleaseGate) -> tuple[bool, list[str]]:
        eligible, reasons = gate.eligible(evaluation)
        blocked = gate.promotion_blocked_reason()
        data = self._all()
        if eligible and blocked:
            cand.status = CandidateStatus.SHADOW
            reasons = [blocked]
        elif eligible:
            cand.status = CandidateStatus.ELIGIBLE
        else:
            cand.status = CandidateStatus.REJECTED
        cand.reasons = reasons
        self._update_candidate(data, cand)
        self._write(data)
        return (cand.status is CandidateStatus.ELIGIBLE), reasons

    def release(self, cand: Candidate, evaluation: EvaluationReference, gate: ReleaseGate, *, source_hashes: Optional[dict[str, str]] = None, dependencies: Optional[list[str]] = None, model_configuration: Optional[dict[str, Any]] = None) -> Optional[ReleaseManifest]:
        if cand.status is not CandidateStatus.ELIGIBLE:
            return None
        data = self._all()
        manifest = ReleaseManifest(
            candidate_id=cand.id,
            name=cand.name,
            source_hashes=dict(source_hashes or {}),
            dependencies=list(dependencies or []),
            model_configuration=dict(model_configuration or {}),
            evaluation_reference_id=evaluation.id,
            contract_fingerprint=gate.fingerprint,
            rollback_to=data.get("active"),
            scope=ReleaseScope.LOCAL,
        )
        data["releases"].append(manifest.model_dump(mode="json"))
        # Transactional: the candidate becomes active and the pointer moves in one write.
        data["active"] = manifest.id
        cand.status = CandidateStatus.ACTIVE
        cand.release_id = manifest.id
        self._update_candidate(data, cand)
        self._write(data)
        return manifest

    def rollback(self, reason: str) -> Optional[ReleaseManifest]:
        data = self._all()
        active_id = data.get("active")
        if not active_id:
            return None
        current = next((ReleaseManifest.model_validate(r) for r in data["releases"] if r["id"] == active_id), None)
        if current is None or not current.rollback_to:
            data["active"] = None
            self._write(data)
            return None
        target = next((ReleaseManifest.model_validate(r) for r in data["releases"] if r["id"] == current.rollback_to), None)
        data["active"] = current.rollback_to
        for entry in data["candidates"]:
            if entry.get("release_id") == active_id:
                entry["status"] = CandidateStatus.ROLLED_BACK.value
                entry["reasons"] = [reason]
        self._write(data)
        return target

    def active_release(self) -> Optional[ReleaseManifest]:
        data = self._all()
        active_id = data.get("active")
        if not active_id:
            return None
        return next((ReleaseManifest.model_validate(r) for r in data["releases"] if r["id"] == active_id), None)

    def candidates(self) -> list[Candidate]:
        return [Candidate.model_validate(c) for c in self._all()["candidates"]]

    @staticmethod
    def _update_candidate(data: dict[str, Any], cand: Candidate) -> None:
        for entry in data["candidates"]:
            if entry["id"] == cand.id:
                entry.clear()
                entry.update(cand.model_dump(mode="json"))
                return
        data["candidates"].append(cand.model_dump(mode="json"))


def evaluate_candidate_against(
    contract: GradingContract,
    *,
    run_baseline: Callable[[], tuple[float, list[str], list[str]]],
    run_candidate: Callable[[], tuple[float, list[str], list[str]]],
    retention_delta: float = 0.0,
) -> EvaluationReference:
    """Run both sides of the comparison against the same fixed contract.

    Both callables must actually execute; the reference records `executed` only when a run
    happened, so a candidate that produced a number without doing anything cannot be released.
    """
    baseline_score, _, _ = run_baseline()
    candidate_score, passed, failed = run_candidate()
    return EvaluationReference(
        contract_id=contract.id,
        contract_fingerprint=contract.fingerprint(),
        baseline_score=baseline_score,
        candidate_score=candidate_score,
        checks_passed=list(passed),
        checks_failed=list(failed),
        retention_delta=retention_delta,
        executed=True,
    )
