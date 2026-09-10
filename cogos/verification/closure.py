"""Criterion closure: finish the work you have already done (live-run area E).

The live mission solved its task. `calc.py` and `test_calc.py` were on disk and correct — 4/4 tests
pass when run independently. It then spent eleven cycles and $20.51 never binding a single
verification receipt, and the completion gate correctly refused for want of one.

The machinery was not missing. The mission's own plan held **eight** ready or pending verification
tasks, including one whose parameters were literally
``{"cwd": "/tmp/cogos-live-run", "commands": ["…/python -m pytest -q"]}`` addressing the exact
criterion the gate was failing on. Selection never reached any of them: a saturated contradiction
kept the controller issuing `must_falsify`, and the planner's ordering gave a verification task only
a +0.15 nudge.

So this module does not verify anything, and deliberately so. It reads the completion gate's *own*
failed checks, works out which already-planned tasks would bind evidence to them, and makes those
tasks dominate the ordering. Verification still runs through the verification engine and produces
genuine receipts exactly as before.

The distinction matters: this changes **which action is chosen**, never **what counts as evidence**.
Nothing here can satisfy a criterion, register an artifact, or manufacture a receipt.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

#: Operation hints whose whole job is to produce verification evidence.
VERIFYING_HINTS = frozenset({"verify", "run_tests"})

#: How strongly a closure-relevant task outranks ordinary work once the gate is blocked on it.
#: Large enough to beat the priority + unknown-value + coverage terms combined, because a mission
#: that has done the work and cannot close is in a strictly worse state than one still exploring.
CLOSURE_BONUS = 1.25


class ClosureNeed(BaseModel):
    """One unsatisfied completion predicate, taken verbatim from the gate."""

    check: str = Field(description="The failing gate check's name")
    detail: str = Field(default="", description="What the gate said was missing")
    criterion_ids: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    wants_verification: bool = Field(default=False, description="Whether running a verification would bind the missing evidence")


class ClosureState(BaseModel):
    """What is standing between the mission and an honest completion."""

    blocked: bool = False
    needs: list[ClosureNeed] = Field(default_factory=list)
    candidate_task_ids: list[str] = Field(default_factory=list)
    substantive_work_done: bool = False

    def summary(self) -> str:
        if not self.blocked:
            return "completion not blocked on missing evidence"
        parts = [f"{n.check}: {n.detail[:120]}" for n in self.needs[:3]]
        return f"{len(self.needs)} completion predicate(s) unmet — " + "; ".join(parts)


def needs_from_gate(gate: Any, state: Any) -> list[ClosureNeed]:
    """Translate the gate's failed checks into evidence that is missing.

    Reads the gate's structured output rather than re-deriving completion logic, so this can never
    drift from what the gate actually requires.
    """
    needs: list[ClosureNeed] = []
    for check in getattr(gate, "checks", []) or []:
        if getattr(check.status, "value", check.status) != "failed":
            continue
        need = ClosureNeed(check=check.name, detail=check.detail or "")
        if check.name == "success_criteria":
            need.criterion_ids = [
                c.id
                for c in state.success_criteria
                if not (c.satisfied and state.passing_verifications(c.verification_ids, target_type="criterion", target_id=c.id))
            ]
            need.wants_verification = True
        elif check.name == "required_artifacts":
            need.artifact_refs = [str(r) for r in (state.resources.get("required_artifacts") or [])]
            need.wants_verification = True
        elif check.name == "tests":
            need.wants_verification = True
        needs.append(need)
    return needs


def substantive_work_done(state: Any) -> bool:
    """Whether the mission's non-verification work has been carried out.

    Used only to decide whether the mission *should* be closing; it never asserts that the work was
    correct — that is exactly what verification is for.
    """
    producing = [t for t in state.tasks if t.operation_hint not in VERIFYING_HINTS]
    if not producing:
        return False
    unfinished = [t for t in producing if t.status.value in ("pending", "ready", "active")]
    return not unfinished or bool(state.artifacts)


def task_addresses(task: Any, needs: list[ClosureNeed]) -> Optional[str]:
    """Why this task would help close, or None if it would not.

    Generic by construction: a task qualifies through the criteria it declares it addresses, or by
    being a verifying operation while the gate wants verification. No domain knowledge.
    """
    if not needs:
        return None
    wanted_criteria = {cid for n in needs for cid in n.criterion_ids}
    if wanted_criteria & set(getattr(task, "addresses_criterion_ids", []) or []):
        return f"addresses {len(wanted_criteria & set(task.addresses_criterion_ids))} unverified criterion(s)"
    if task.operation_hint in VERIFYING_HINTS and any(n.wants_verification for n in needs):
        return f"a {task.operation_hint} step would bind the evidence the gate is missing"
    return None


def assess(state: Any, gate: Any, planner: Any = None) -> ClosureState:
    """Work out whether the mission is in closure, and which planned tasks would close it."""
    status = getattr(gate, "status", None)
    blocked = getattr(status, "value", status) == "failed"
    needs = needs_from_gate(gate, state) if blocked else []
    closure = ClosureState(blocked=blocked, needs=needs, substantive_work_done=substantive_work_done(state))
    if not blocked or not needs:
        return closure
    candidates = []
    for task in state.tasks:
        if task.status.value in ("done", "cancelled", "failed"):
            continue
        if task_addresses(task, needs):
            candidates.append(task.id)
    closure.candidate_task_ids = candidates
    return closure


def closure_bonus(task: Any, closure: Optional[ClosureState], needs: list[ClosureNeed]) -> float:
    """Ordering boost for a task that would bind missing completion evidence.

    Applies only while the gate is actually blocked on that evidence — an unblocked mission gets no
    distortion, and the boost disappears the moment the need is met.
    """
    if closure is None or not closure.blocked or not needs:
        return 0.0
    return CLOSURE_BONUS if task_addresses(task, needs) else 0.0
