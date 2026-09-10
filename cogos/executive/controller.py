"""Meta-cognitive controller.

Estimates the state of cognition (understanding, uncertainty, stakes,
contradiction level, progress rate, tool/memory reliability, likelihood the
current strategy is wrong, expected value of additional work) and converts
those estimates into a small set of *directives* that shape the next step.
It exists to improve action selection, not to generate commentary.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from cogos.schemas.mission import MissionState, TaskStatus


class Assessment(BaseModel):
    mission_understanding: float = 0.5
    confidence: float = 0.0
    uncertainty: float = 1.0
    novelty: float = 0.5
    stakes: float = 0.5
    contradiction_level: float = 0.0
    progress: float = 0.0
    progress_rate: float = 0.0
    stalled_cycles: int = 0
    evidence_coverage: float = 0.0
    tool_reliability: float = 1.0
    memory_reliability: float = 0.7
    p_strategy_wrong: float = 0.2
    ev_additional_work: float = 0.5
    budget_pressure: float = 0.0
    directives: list[str] = Field(default_factory=list)
    effort: str = "high"
    notes: list[str] = Field(default_factory=list)


class MetaCognitiveController:
    def __init__(self, stall_threshold: int = 4):
        self.stall_threshold = stall_threshold

    def assess(
        self,
        state: MissionState,
        *,
        tool_results_history: Optional[list[bool]] = None,
        memory_hits: Optional[tuple[int, int]] = None,
        belief_summary: Optional[dict[str, Any]] = None,
        verification_pending: Optional[list[str]] = None,
        falsification_target: Optional[dict[str, Any]] = None,
        challenged: bool = False,
        over_budget: Optional[str] = None,
    ) -> Assessment:
        a = Assessment()
        ctl = state.resources.setdefault("controller", {})
        history: list[dict[str, Any]] = ctl.setdefault("history", [])

        crit = state.success_criteria
        a.progress = state.progress
        a.mission_understanding = 0.4 + 0.6 * (1.0 if crit else 0.0) * min(1.0, 0.5 + 0.1 * len(state.tasks))
        open_unknowns = state.open_unknowns()
        total_unknowns = len(state.unknowns)
        high_value_open = [u for u in open_unknowns if u.priority() >= 1.0]
        a.uncertainty = round(min(1.0, (len(open_unknowns) / total_unknowns) if total_unknowns else 0.5), 3)
        a.evidence_coverage = round(1.0 - a.uncertainty, 3)
        a.confidence = round(max(state.confidence, 0.1 * len(state.completed_tasks()) / max(1, len(state.tasks))) if state.tasks else state.confidence, 3)
        ctr = state.unresolved_contradictions()
        a.contradiction_level = round(min(1.0, sum(c.severity for c in ctr)), 3)
        a.stakes = round(max([r.probability * r.impact for r in state.risks] + [0.4 if crit else 0.3]), 3)
        a.novelty = round(0.7 if not state.learned_lessons and state.usage.cycles < 3 else max(0.2, 0.7 - 0.05 * state.usage.cycles), 3)

        if tool_results_history:
            a.tool_reliability = round(sum(1 for ok in tool_results_history[-10:] if ok) / len(tool_results_history[-10:]), 3)
        if memory_hits and memory_hits[1]:
            a.memory_reliability = round(memory_hits[0] / memory_hits[1], 3)

        # progress rate / stall detection from history of (cycle, progress, completed)
        completed = len(state.completed_tasks())
        if history:
            last = history[-1]
            delta = completed - int(last.get("completed", 0))
            a.progress_rate = round(state.progress - float(last.get("progress", 0.0)), 3)
            stalled = int(last.get("stalled_cycles", 0)) + 1 if delta == 0 and a.progress_rate <= 0 else 0
        else:
            stalled = 0
        a.stalled_cycles = stalled

        failed = state.failed_tasks()
        repeated = [t for t in failed if t.attempts >= 2]
        a.p_strategy_wrong = round(min(0.95, 0.1 + 0.15 * len(repeated) + 0.1 * (stalled >= self.stall_threshold) + 0.2 * a.contradiction_level), 3)
        a.ev_additional_work = round(max(0.0, (1.0 - state.progress) * (0.5 + 0.5 * a.uncertainty) * (1.0 - 0.5 * a.p_strategy_wrong)), 3)
        if over_budget:
            a.budget_pressure = 1.0
        else:
            b = state.budget
            a.budget_pressure = round(max(state.usage.cycles / max(1, b.max_cycles), state.usage.model_calls / max(1, b.max_model_calls)), 3)

        # ---- directives (hard rules first) ----
        d: list[str] = []
        if over_budget:
            d.append(f"stop:{over_budget}")
        blocked_unresolved = [b for b in state.blocked_operations if not b.resolved]
        ready = [t for t in state.tasks if t.status in (TaskStatus.READY, TaskStatus.PENDING)]
        if state.unanswered_human_requests() and not ready:
            d.append("escalate:no independent work remains; a human decision is required")
        if verification_pending:
            d.append(f"must_verify:{len(verification_pending)} completed task(s) have unverified outputs")
        if falsification_target and a.contradiction_level >= 0.5:
            d.append("must_falsify:serious contradiction — run targeted falsification before relying on the leading belief")
        elif falsification_target and falsification_target.get("priority", 0) >= 0.7 and not falsification_target.get("attempted"):
            d.append("should_falsify:high-stakes leading belief has not been challenged")
        if a.stakes >= 0.5 and not challenged and state.progress >= 0.6:
            d.append("should_challenge:consequential conclusion pending; obtain independent reasoning before synthesis")
        if stalled >= self.stall_threshold or len(repeated) >= 2:
            d.append("change_strategy:progress stalled or repeated structural failures; abandon the failing approach")
        for t in failed:
            if t.attempts >= t.max_attempts:
                d.append(f"terminate_branch:{t.id} exhausted attempts")
        if blocked_unresolved:
            d.append(f"isolate_blocked:{len(blocked_unresolved)} operation(s) blocked; continue unaffected work")
        if high_value_open and state.progress < 0.8:
            d.append(f"prioritise_unknowns:{len(high_value_open)} decision-changing unknown(s) open")
        if a.tool_reliability < 0.5 and tool_results_history and len(tool_results_history) >= 4:
            d.append("tool_unreliable:recent tool failures high; prefer alternative substrate or diagnose")
        if a.ev_additional_work < 0.05 and state.progress >= 0.9:
            d.append("diminishing_returns:additional work has negligible expected value; move to synthesis/completion")
        a.directives = d
        a.effort = "max" if (a.novelty >= 0.6 and a.confidence < 0.4) or a.contradiction_level >= 0.5 or a.stakes >= 0.7 else "high"
        if a.stakes >= 0.7:
            a.notes.append("high stakes: verification depth and independent challenge required")

        history.append({"cycle": state.usage.cycles, "progress": state.progress, "completed": completed, "stalled_cycles": stalled, "uncertainty": a.uncertainty, "contradiction": a.contradiction_level, "p_wrong": a.p_strategy_wrong})
        del history[:-50]
        return a
