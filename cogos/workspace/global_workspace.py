"""Global Workspace: a compact, regenerated digest of what matters right now.

It is rebuilt from deep state every cycle and bounded in size. It is what the
executive model sees when choosing the next step — never a transcript dump.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from cogos.schemas.mission import MissionState, TaskStatus


class WorkspaceView(BaseModel):
    text: str
    sections: dict[str, list[str]] = Field(default_factory=dict)
    truncated: bool = False
    char_count: int = 0


class GlobalWorkspace:
    def __init__(self, max_chars: int = 12_000):
        self.max_chars = max_chars

    def build(
        self,
        state: MissionState,
        *,
        ready_tasks: Optional[list[Any]] = None,
        belief_lines: Optional[list[str]] = None,
        world_lines: Optional[list[str]] = None,
        memory_lines: Optional[list[str]] = None,
        recent_observations: Optional[list[str]] = None,
        assessment: Optional[dict[str, Any]] = None,
        directives: Optional[list[str]] = None,
        tools_text: str = "",
        skills_text: str = "",
    ) -> WorkspaceView:
        sections: dict[str, list[str]] = {}
        sections["mission"] = [
            f"id={state.mission_id} status={state.status.value} progress={state.progress:.2f} confidence={state.confidence:.2f}",
            f"objective: {state.objective}",
            f"executive_model={state.executive_model or 'unset'} cycles={state.usage.cycles} model_calls={state.usage.model_calls} tool_calls={state.usage.tool_calls}",
        ]
        if state.explicit_constraints or state.inferred_constraints:
            sections["constraints"] = [f"[explicit] {c}" for c in state.explicit_constraints[:6]] + [f"[inferred] {c}" for c in state.inferred_constraints[:6]]
        sections["success_criteria"] = [
            f"{'[x]' if c.satisfied else '[ ]'} {c.id}: {c.description} (verify: {c.verification_method or 'unspecified'})"
            for c in state.success_criteria[:10]
        ]
        goals = [g for g in state.goals if g.status not in (TaskStatus.DONE, TaskStatus.CANCELLED)]
        if goals:
            sections["goals"] = [f"{g.id} [{g.level}] {g.title}" for g in goals[:8]]
        if ready_tasks:
            sections["ready_tasks"] = [
                f"{t.id} p={t.priority:.2f} attempts={t.attempts} hint={t.operation_hint or '-'}: {t.title}"
                + (f" — {t.description[:160]}" if t.description else "")
                for t in ready_tasks[:8]
            ]
        active = [t for t in state.tasks if t.status == TaskStatus.ACTIVE]
        if active:
            sections["active_tasks"] = [f"{t.id}: {t.title}" for t in active[:6]]
        failed = [t for t in state.tasks if t.status == TaskStatus.FAILED]
        if failed:
            sections["failed_tasks"] = [f"{t.id} attempts={t.attempts}: {t.title} — {t.failure_reason[:160]}" for t in failed[:5]]
        unknowns = sorted(state.open_unknowns(), key=lambda u: -u.priority())[:8]
        if unknowns:
            sections["important_unknowns"] = [f"{u.id} prio={u.priority():.2f} attempts={u.attempts}: {u.question}" for u in unknowns]
        if belief_lines:
            sections["high_impact_beliefs"] = belief_lines[:10]
        hyps = [h for h in state.hypotheses if h.status in ("active", "leading")]
        if hyps:
            sections["active_hypotheses"] = [f"{h.id} [{h.status} {h.confidence:.2f}] {h.statement}" for h in hyps[:8]]
        ctr = state.unresolved_contradictions()
        if ctr:
            sections["contradictions"] = [f"{c.id} sev={c.severity:.2f} cause={c.suspected_cause}: {c.description[:200]}" for c in ctr[:5]]
        if world_lines:
            sections["world_model"] = world_lines[:10]
        blocked = [b for b in state.blocked_operations if not b.resolved]
        if blocked:
            sections["blocked_operations"] = [f"{b.id} [{b.action_class.value}] {b.operation}: {b.reason} — unblock: {b.what_would_unblock}" for b in blocked[:5]]
        hr = state.unanswered_human_requests()
        if hr:
            sections["pending_human_requests"] = [f"{h.id} [{h.kind}] {h.question}" for h in hr[:5]]
        if state.commitments:
            sections["commitments"] = [f"{'[x]' if c.fulfilled else '[ ]'} {c.statement}" for c in state.commitments[:5]]
        if state.decisions:
            sections["recent_decisions"] = [f"{d.decision_id} ({d.confidence:.2f}): {d.selected_option[:120]} — {d.concise_rationale[:140]}" for d in state.decisions[-4:]]
        if memory_lines:
            sections["relevant_memory"] = memory_lines[:8]
        if recent_observations:
            sections["recent_observations"] = recent_observations[-8:]
        if state.learned_lessons:
            sections["lessons"] = [f"[{l.category}] {l.statement[:160]}" for l in state.learned_lessons[-5:]]
        if assessment:
            sections["assessment"] = [f"{k}={v}" for k, v in assessment.items()]
        if directives:
            sections["controller_directives"] = list(directives)
        if state.synthesis:
            sections["current_synthesis"] = [f"{k}: {str(v)[:220]}" for k, v in state.synthesis.items() if k in ("conclusion", "decision", "confidence", "mission_status")]
        if skills_text:
            sections["relevant_skills"] = skills_text.splitlines()[:12]
        if tools_text:
            sections["available_tools"] = tools_text.splitlines()

        text, truncated = self._render(sections)
        return WorkspaceView(text=text, sections=sections, truncated=truncated, char_count=len(text))

    def _render(self, sections: dict[str, list[str]]) -> tuple[str, bool]:
        # Allocate space proportionally with a per-section floor so nothing important disappears.
        order = list(sections.keys())
        budget = self.max_chars
        out: list[str] = []
        truncated = False
        per_section = max(400, budget // max(1, len(order)))
        for name in order:
            lines = sections[name]
            block = f"## {name}\n" + "\n".join(f"- {line}" for line in lines)
            if len(block) > per_section:
                block = block[: per_section - 20] + "\n- …[truncated]"
                truncated = True
            out.append(block)
        text = "\n\n".join(out)
        if len(text) > budget:
            text = text[: budget - 15] + "\n…[truncated]"
            truncated = True
        return text, truncated
