"""Progress measured by what can be checked from outside (live-run area G).

The live controller did notice that four cycles had produced nothing and changed strategy — that
behaviour is worth keeping. What it could not see is subtler and was the actual story of the run:
spend climbed steadily across eleven cycles while **satisfied criteria stayed at zero and bound
receipts stayed at zero the entire time**. Files were written, claims accumulated, contradictions
multiplied; none of it was progress toward a completion anyone could verify.

So progress here is the vector of things an outside party could check: criteria with a bound
passing receipt, verified artifacts that still match their hash, passing test records, resolved
blockers, answered unknowns, settled contradictions. Writing a file is activity; a criterion with
a receipt behind it is progress.

When spend rises and that vector does not move, the runtime says so — as an observation, not an
instruction. The executive decides what to do about it. Forcing a particular action here would be
the runtime overriding judgment, which is the opposite of what the live run needed.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

#: Cycles of rising spend against a flat progress vector before it is worth saying so.
FLAT_PROGRESS_CYCLES = 3

#: Spend increase (USD) over that window below which "rising" is not a meaningful word.
MATERIAL_SPEND_USD = 0.5


class VerifiableProgress(BaseModel):
    """Externally checkable outcomes. Every field is something a third party could confirm."""

    criteria_with_receipts: int = 0
    verified_artifacts: int = 0
    passing_tests: int = 0
    resolved_blockers: int = 0
    resolved_unknowns: int = 0
    settled_contradictions: int = 0
    completed_tasks: int = 0

    def vector(self) -> tuple[int, ...]:
        return (
            self.criteria_with_receipts,
            self.verified_artifacts,
            self.passing_tests,
            self.resolved_blockers,
            self.resolved_unknowns,
            self.settled_contradictions,
            self.completed_tasks,
        )

    def total(self) -> int:
        return sum(self.vector())


def measure(state: Any) -> VerifiableProgress:
    """Read the current verifiable-progress vector off mission state."""
    from cogos.verification.engine import artifact_integrity

    criteria = 0
    for c in state.success_criteria:
        if c.satisfied and state.passing_verifications(c.verification_ids, target_type="criterion", target_id=c.id):
            criteria += 1

    artifacts = 0
    for a in state.artifacts:
        if a.verified and artifact_integrity(a)[0]:
            artifacts += 1

    latest: dict[str, Any] = {}
    for rec in state.tests:
        if not rec.expected_failure:
            latest[rec.name] = rec
    passing = sum(1 for t in latest.values() if getattr(t.status, "value", t.status) == "passed")

    return VerifiableProgress(
        criteria_with_receipts=criteria,
        verified_artifacts=artifacts,
        passing_tests=passing,
        resolved_blockers=sum(1 for b in state.blocked_operations if b.resolved),
        resolved_unknowns=sum(1 for u in state.unknowns if u.resolved),
        settled_contradictions=sum(1 for c in state.contradictions if c.resolved),
        completed_tasks=len(state.completed_tasks()),
    )


class ProgressObservation(BaseModel):
    """What the runtime noticed about the relationship between spend and progress."""

    flat: bool = False
    cycles_flat: int = 0
    spend_since_flat_usd: float = 0.0
    model_calls_since_flat: int = 0
    current: VerifiableProgress = Field(default_factory=VerifiableProgress)
    note: str = ""


def track(state: Any) -> ProgressObservation:
    """Update the mission's spend-versus-progress record and report what it shows.

    Stored on mission state, so the observation survives checkpoint and resume rather than being
    an artefact of one process's memory.
    """
    current = measure(state)
    book = state.resources.setdefault("progress", {})
    previous = tuple(book.get("vector") or ())
    spend_at_mark = float(book.get("spend_at_mark", state.usage.estimated_cost_usd))
    calls_at_mark = int(book.get("calls_at_mark", state.usage.model_calls))
    cycles_flat = int(book.get("cycles_flat", 0))

    if tuple(current.vector()) != previous:
        # Something checkable moved: reset the window and mark where spend stood.
        cycles_flat = 0
        spend_at_mark = state.usage.estimated_cost_usd
        calls_at_mark = state.usage.model_calls
    else:
        cycles_flat += 1

    book.update(
        {
            "vector": list(current.vector()),
            "cycles_flat": cycles_flat,
            "spend_at_mark": spend_at_mark,
            "calls_at_mark": calls_at_mark,
            "detail": current.model_dump(),
        }
    )

    spend_since = round(state.usage.estimated_cost_usd - spend_at_mark, 6)
    calls_since = state.usage.model_calls - calls_at_mark
    observation = ProgressObservation(
        cycles_flat=cycles_flat,
        spend_since_flat_usd=spend_since,
        model_calls_since_flat=calls_since,
        current=current,
    )
    if cycles_flat >= FLAT_PROGRESS_CYCLES and (spend_since >= MATERIAL_SPEND_USD or calls_since >= FLAT_PROGRESS_CYCLES):
        observation.flat = True
        observation.note = (
            f"{cycles_flat} cycles without externally verifiable progress while spending "
            f"${spend_since:.2f} over {calls_since} model call(s). Verifiable progress means a criterion with a "
            f"bound receipt, a verified artifact, a passing test, a resolved blocker or unknown, a settled "
            f"contradiction — currently {current.total()} in total. Activity is not progress; consider whether the "
            f"current approach can produce checkable evidence, and if not, choose a different one."
        )
    return observation
