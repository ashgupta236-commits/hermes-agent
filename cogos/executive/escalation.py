"""Cognitive escalation control: use the cheapest mechanism that can actually answer.

The live run spent 70% of its budget on `interpret` — nine calls, $13.69, 185k output tokens — and
the cost was completely decoupled from the size of what was being interpreted. A 306ms
`search_text` triggered a 423-second, $2.28 interpretation; a 64ms `shell` call triggered a
486-second, $2.37 one. Frontier deliberation was the default handler for observations a few lines
of Python can read.

The ladder here says which mechanism an observation deserves:

======  ==========================================================================
``L0``  Deterministic. The runtime already knows what happened; no model is called.
``L1``  Digest. A bounded call with a reduced schema — enough to read the result and
        update the task, structurally unable to emit a belief-graph essay.
``L2``  Full interpretation. The whole :class:`ObservationInterpretation` contract,
        for observations that genuinely bear on beliefs, contradictions or unknowns.
======  ==========================================================================

The classification is conservative by construction: **anything that could carry judgment
escalates**. Failures, injected content, untrusted external text, specialist reports, calculation
or simulation output, judgment operations, and any observation arriving while a serious
contradiction is open all go to L2. What drops to L0/L1 is the mechanical residue — a tool that
succeeded, from a trusted substrate, with nothing ambiguous about it.

This is reallocation, not compression. Nothing here reduces the cognition available where judgment
has value; it stops spending frontier deliberation where judgment has none.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any, Optional

from pydantic import BaseModel, Field

from cogos.schemas.common import OperationKind, TrustLevel

#: A contradiction at or above this severity means the mission is in a contested state, and even a
#: routine observation may bear on it. Matches the controller's own escalation floor.
CONTESTED_SEVERITY = 0.5

#: Operations whose whole purpose is judgment. These never drop below full interpretation.
JUDGMENT_OPERATIONS = frozenset(
    {
        OperationKind.FALSIFY,
        OperationKind.INSTANTIATE_SPECIALIST,
        OperationKind.PARALLEL_WORKSTREAMS,
        OperationKind.SIMULATE,
        OperationKind.RUN_EXPERIMENT,
        OperationKind.DIRECT_REASONING,
    }
)


class Tier(IntEnum):
    L0_DETERMINISTIC = 0
    L1_DIGEST = 1
    L2_FULL = 2


class Escalation(BaseModel):
    """Why an observation was handled at the tier it was."""

    tier: Tier
    reasons: list[str] = Field(default_factory=list, description="What forced escalation, or why none was needed")

    def describe(self) -> str:
        return f"{self.tier.name}: " + ("; ".join(self.reasons[:4]) if self.reasons else "no escalation trigger")


def _has(attr: Any) -> bool:
    return attr is not None and attr != "" and attr != [] and attr != {}


def classify(
    operation: Any,
    outcome: Any,
    task: Any,
    state: Any,
) -> Escalation:
    """Decide the cheapest tier that can honestly handle this observation.

    Every branch that escalates names its reason, so the choice is auditable in the trace rather
    than being an invisible heuristic.
    """
    reasons: list[str] = []

    if operation in JUDGMENT_OPERATIONS:
        return Escalation(tier=Tier.L2_FULL, reasons=[f"operation '{getattr(operation, 'value', operation)}' exists to produce judgment"])

    results = list(getattr(outcome, "tool_results", []) or [])

    # Anything that went wrong needs a mind on it.
    if getattr(outcome, "errors", None):
        reasons.append("the operation reported errors")
    failed = [r for r in results if not r.ok]
    if failed:
        reasons.append(f"{len(failed)} tool call(s) failed: {', '.join(sorted({r.tool for r in failed}))}")
    flagged = [r for r in results if r.injection_flags]
    if flagged:
        reasons.append(f"injection flags on {', '.join(sorted({r.tool for r in flagged}))}")
    if getattr(outcome, "specialist_reports", None):
        reasons.append("a specialist report needs independent assessment")
    if _has(getattr(outcome, "reasoning_output", "")):
        reasons.append("the operation produced reasoning to integrate")
    if _has(getattr(outcome, "calculation_result", None)) or _has(getattr(outcome, "calculation_error", "")):
        reasons.append("a calculation result needs interpretation")
    if _has(getattr(outcome, "simulation_result", None)):
        reasons.append("a simulation result needs interpretation")
    if getattr(outcome, "untrusted", None):
        reasons.append("untrusted content must be assessed, never absorbed")

    # A mission in a contested state cannot afford a mechanical reading of anything.
    contested = [c for c in state.unresolved_contradictions() if c.severity >= CONTESTED_SEVERITY] if hasattr(state, "unresolved_contradictions") else []
    if contested:
        reasons.append(f"{len(contested)} unresolved contradiction(s) above severity {CONTESTED_SEVERITY} may bear on this")

    # If this step was meant to resolve an open question, deciding whether it did is judgment.
    if task is not None and getattr(task, "resolves_unknown_ids", None):
        reasons.append("the task was chosen to resolve an open unknown")

    if reasons:
        return Escalation(tier=Tier.L2_FULL, reasons=reasons)

    if not results:
        # Nothing mechanical to read and nothing flagged: fall back rather than guess.
        return Escalation(tier=Tier.L2_FULL, reasons=["no tool result to read mechanically"])

    untrusted = [r for r in results if r.trust == TrustLevel.UNTRUSTED_EXTERNAL and r.output]
    if untrusted:
        # The content still has to be read by a mind, but reading a file listing does not justify
        # revising the belief graph. A bounded digest is the right size of instrument.
        return Escalation(
            tier=Tier.L1_DIGEST,
            reasons=[f"content from {', '.join(sorted({r.tool for r in untrusted}))} needs reading, but nothing indicates it bears on beliefs"],
        )

    return Escalation(tier=Tier.L0_DETERMINISTIC, reasons=[])


class ObservationDigest(BaseModel):
    """The reduced interpretation contract used at :attr:`Tier.L1_DIGEST`.

    Deliberately missing the belief-graph fields — claims, contradictions, hypotheses, world model.
    A digest cannot revise what the mission believes, which is both the cost saving and the safety
    property: an observation routed here was classified as *not* bearing on beliefs, so it must not
    be able to change them by accident.
    """

    summary: str
    task_status: str = Field(default="done", description="done|failed|blocked|active|pending")
    result_summary: str = ""
    failure_reason: str = ""
    observed: list[str] = Field(default_factory=list, description="Concrete facts read directly off the tool output")
    resolved_unknown_ids: list[str] = Field(default_factory=list)
    progress_estimate: float = 0.0


def deterministic_interpretation(operation: Any, outcome: Any, task: Any, state: Any) -> Any:
    """Build the interpretation for a wholly mechanical observation, with no model call.

    Records exactly what happened and nothing more. It never satisfies a criterion, never creates a
    claim and never resolves an unknown — those are judgments, and a step that reaches this
    function has been classified as carrying none.
    """
    from cogos.schemas.cognition import EvidenceSpec, ObservationInterpretation, TaskUpdateSpec

    results = list(getattr(outcome, "tool_results", []) or [])
    tools = ", ".join(sorted({r.tool for r in results})) or "no tools"
    summary = f"{getattr(operation, 'value', operation)}: {len(results)} tool call(s) succeeded ({tools})"

    interp = ObservationInterpretation(summary=summary, progress_estimate=getattr(state, "progress", 0.0))
    if task is not None:
        interp.task_updates.append(
            TaskUpdateSpec(task_id=task.id, status="done", result_summary=summary[:300])
        )
    for r in results:
        detail = (r.output or "").strip()
        interp.new_evidence.append(
            EvidenceSpec(
                summary=f"{r.tool} succeeded: {detail[:200]}" if detail else f"{r.tool} succeeded",
                source=f"tool:{r.tool}",
                kind="primary",
                reliability=0.9,
            )
        )
    return interp


def digest_to_interpretation(digest: ObservationDigest, task: Any, state: Any) -> Any:
    """Widen a bounded digest back into the interpretation the loop expects.

    Only the fields the digest was allowed to carry cross over. There is no path here for a digest
    to satisfy a criterion or revise a belief.
    """
    from cogos.schemas.cognition import EvidenceSpec, ObservationInterpretation, TaskUpdateSpec

    interp = ObservationInterpretation(
        summary=digest.summary,
        progress_estimate=digest.progress_estimate or getattr(state, "progress", 0.0),
        resolved_unknowns=list(digest.resolved_unknown_ids),
    )
    if task is not None:
        interp.task_updates.append(
            TaskUpdateSpec(
                task_id=task.id,
                status=digest.task_status or "done",
                result_summary=(digest.result_summary or digest.summary)[:300],
                failure_reason=digest.failure_reason[:300],
            )
        )
    for fact in digest.observed[:10]:
        interp.new_evidence.append(EvidenceSpec(summary=fact[:200], source="tool:observation", kind="primary", reliability=0.85))
    return interp
