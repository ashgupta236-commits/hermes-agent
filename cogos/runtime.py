"""Runtime wiring and the boot/recovery protocol."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from cogos.adapters import ExecutiveModel, build_adapter
from cogos.agent_foundry import AgentFoundry
from cogos.config import CogosConfig, load_config
from cogos.events import EventBus
from cogos.executive import Executive
from cogos.governance import CapabilityFirewall
from cogos.ids import iso_now
from cogos.memory import MemoryManager
from cogos.mission import MissionCompiler
from cogos.observability import Tracer
from cogos.persistence import StateStore
from cogos.planner import Planner
from cogos.schemas.events import Event
from cogos.schemas.mission import Budget, MissionState, MissionStatus, TaskStatus
from cogos.skills import SkillCompiler
from cogos.skills.loader import discover, relevant_skills, render_for_prompt
from cogos.tools import ToolFabric, build_default_fabric
from cogos.tools.fabric import ToolContext
from cogos.workspace import GlobalWorkspace


class BootReport(BaseModel):
    repo_root: str
    cogos_home: str
    store_health: dict[str, Any]
    missions: list[dict[str, Any]] = Field(default_factory=list)
    resume_target: Optional[str] = None
    resume_reason: str = ""
    git_status: str = ""
    git_log: str = ""
    unresolved_tasks: list[str] = Field(default_factory=list)
    latest_verification: list[str] = Field(default_factory=list)
    pending_events: list[str] = Field(default_factory=list)
    human_requests: list[str] = Field(default_factory=list)
    environment: dict[str, Any] = Field(default_factory=dict)
    workspace: str = ""
    warnings: list[str] = Field(default_factory=list)
    booted_at: str = Field(default_factory=iso_now)


class Runtime:
    def __init__(self, config: Optional[CogosConfig] = None, adapter: Optional[ExecutiveModel] = None, *, stdout_trace: Optional[bool] = None):
        self.config = config or load_config()
        self.config.ensure_dirs()
        self.store = StateStore(self.config.db_path)
        self.memory = MemoryManager(self.store, self.config.memory)
        self.events = EventBus(self.store)
        self.firewall = CapabilityFirewall(self.config.governance, Path(self.config.repo_root), extra_writable=[self.config.home])
        self.tool_context = ToolContext(Path(self.config.repo_root), timeout=self.config.governance.shell_timeout_seconds, max_output_chars=self.config.governance.max_output_chars, memory=self.memory)
        self.fabric: ToolFabric = build_default_fabric(self.firewall, self.tool_context)
        self._configure_tool_availability()
        self.adapter: ExecutiveModel = adapter or build_adapter(self.config.executive.adapter, **self._adapter_kwargs())
        self.tracer = Tracer(self.store, stdout=self.config.trace_to_stdout if stdout_trace is None else stdout_trace, jsonl_path=self.config.home / "traces.jsonl")
        self.foundry = AgentFoundry(self.adapter, self.config)
        self.executive = Executive(self.config, self.store, self.adapter, self.fabric, self.memory, self.tracer, self.events, self.foundry)
        self.skills = SkillCompiler(self.store, self.config.skills_dir, claude_skills_dir=Path(self.config.repo_root) / ".claude" / "skills")

    def _adapter_kwargs(self) -> dict[str, Any]:
        ex = self.config.executive
        if ex.adapter == "claude_code":
            return {"model": ex.model, "binary": ex.claude_binary, "max_retries": ex.max_retries, "effort": ex.effort, "extra_args": ex.extra_cli_args}
        if ex.adapter == "anthropic_api":
            return {"model": ex.model, "max_retries": ex.max_retries}
        return {"model": ex.model}

    def _configure_tool_availability(self) -> None:
        if not self.config.governance.allow_network:
            self.fabric.mark_unavailable("web_fetch", "network disabled by policy")
        if not self.config.governance.allow_shell:
            self.fabric.mark_unavailable("shell", "shell disabled by policy")
            self.fabric.mark_unavailable("run_tests", "shell disabled by policy")

    # -- missions ------------------------------------------------------------------

    def _provenance(self, schema_version: int = 0) -> dict[str, Any]:
        """Which implementation is producing this mission's trace (area I)."""
        from cogos.provenance import capture

        return capture(self.config, self.adapter, schema_version).model_dump(mode="json")

    def new_mission(self, objective: str, *, human_context: str = "", budget: Optional[Budget] = None, permissions: Optional[dict[str, Any]] = None, context: Optional[dict[str, Any]] = None) -> MissionState:
        memory_lines = []
        try:
            from cogos.governance.immune import scan_for_injection

            memory_lines = [f"[{m.memory_class.value} {m.confidence:.2f}] {m.content[:200]}" for m in self.memory.retrieve(objective, limit=8) if not scan_for_injection(m.content)]
        except Exception:  # noqa: BLE001
            memory_lines = []
        skills_text = ""
        try:
            skills = discover([self.config.skills_dir, Path(self.config.repo_root) / ".claude" / "skills"])
            rel = relevant_skills(objective, skills, limit=3)
            skills_text = render_for_prompt(rel, full=False) if rel else ""
        except Exception:  # noqa: BLE001
            skills_text = ""
        compiler = MissionCompiler(self.adapter, self.config)
        state, comp, meta = compiler.compile(objective, context=context, human_context=human_context, memory_lines=memory_lines, skills_text=skills_text, budget=budget, permissions=permissions)
        state.resources["skills_text"] = skills_text
        # Area I: bind the trace to the implementation that produced it. The live mission ran
        # while fixes were being committed, so repo HEAD and the loaded code diverged and the
        # trace could not say which build it came from.
        state.resources["provenance"] = self._provenance(state.schema_version)
        state.capability_state = {s.name: {"available": s.available, "reason": s.unavailable_reason} for s in self.fabric.specs(include_unavailable=True)}
        state.permission_state = {"always_require_human": list(self.config.governance.always_require_human), "denied_action_classes": list(self.config.governance.denied_action_classes), "allow_network": self.config.governance.allow_network, "allow_shell": self.config.governance.allow_shell, "grants": list((permissions or {}).get("grants", []))}
        self.store.save_mission(state, "mission_compiled", {"used_fallback": meta["used_fallback"], "mission_kind": comp.mission_kind})
        self.tracer.set_mission(state.mission_id)
        self.tracer.emit("mission_compiled", f"{comp.mission_kind}: {comp.interpretation[:200]}", data={"objective": objective, "mission_kind": comp.mission_kind, "criteria": [c.description for c in comp.success_criteria], "unknowns": [u.question for u in comp.unknowns], "tasks": [t.title for t in comp.tasks], "hypotheses": [h.statement for h in comp.hypotheses], "used_fallback": meta["used_fallback"], "executive_model": state.executive_model}, cost={"cost_usd": meta["response"].get("cost_usd", 0.0), "input_tokens": meta["response"].get("input_tokens", 0), "output_tokens": meta["response"].get("output_tokens", 0)})
        self.store.kv_set("last_mission_id", state.mission_id)
        return state

    def run(self, mission_id: str, max_cycles: Optional[int] = None) -> MissionState:
        state = self.executive.run(mission_id, max_cycles=max_cycles)
        if state.status == MissionStatus.COMPLETE:
            self.try_compile_skill(state.mission_id)
        self.store.kv_set("last_mission_id", state.mission_id)
        return state

    def resume_target(self) -> tuple[Optional[str], str]:
        rows = self.store.list_missions()
        pending = set(self.events.wake_targets())
        ranked: list[tuple[int, str, str]] = []
        for r in rows:
            mid, status = r["mission_id"], r["status"]
            if status == MissionStatus.ACTIVE.value:
                ranked.append((0, mid, "active mission"))
            elif status in (MissionStatus.PAUSED.value, MissionStatus.BLOCKED_EXTERNAL.value) and mid in pending:
                ranked.append((1, mid, f"{status} mission with pending events"))
            elif status == MissionStatus.DRAFT.value:
                ranked.append((2, mid, "compiled but not started"))
            elif status == MissionStatus.PAUSED.value:
                ranked.append((3, mid, "paused mission"))
        if not ranked:
            return None, "no unfinished mission"
        ranked.sort(key=lambda x: x[0])
        return ranked[0][1], ranked[0][2]

    def resume(self, mission_id: Optional[str] = None, max_cycles: Optional[int] = None) -> Optional[MissionState]:
        target, reason = (mission_id, "explicit") if mission_id else self.resume_target()
        if target is None:
            return None
        self.tracer.set_mission(target)
        self.tracer.emit("resume", f"resuming {target}: {reason}")
        return self.run(target, max_cycles=max_cycles)

    # -- human interaction -------------------------------------------------------------

    def answer(self, mission_id: str, request_id: str, answer: str, *, grant: Optional[str] = None) -> Event:
        ev = Event(kind="human_input", source="human", payload={"request_id": request_id, "answer": answer, **({"grant": grant} if grant else {})}, mission_ids=[mission_id], trusted=True)
        self.events.emit(ev)
        state = self.store.load_mission(mission_id)
        if state and state.status in (MissionStatus.BLOCKED_EXTERNAL, MissionStatus.PAUSED):
            state.status = MissionStatus.ACTIVE
            state.notes.append(f"resumed by human input {ev.id}")
            self.store.save_mission(state, "human_input", {"event": ev.id})
        return ev

    def authorize(self, mission_id: str, action_class: str) -> MissionState:
        state = self.store.load_mission(mission_id)
        if state is None:
            raise KeyError(mission_id)
        state.permissions.setdefault("grants", [])
        if action_class not in state.permissions["grants"]:
            state.permissions["grants"].append(action_class)
        state.permission_state["grants"] = list(state.permissions["grants"])
        if state.status in (MissionStatus.BLOCKED_EXTERNAL, MissionStatus.PAUSED):
            state.status = MissionStatus.ACTIVE
        # One implementation of "a grant unblocks this": the executive's, which also keeps
        # the reactivated task inside its attempt budget.
        self.executive._apply_grants(state)
        self.store.save_mission(state, "authorized", {"action_class": action_class})
        self.tracer.set_mission(mission_id)
        self.tracer.emit("event", f"human authorized action class '{action_class}'", data={"action_class": action_class})
        return state

    def correct(self, mission_id: str, correction: str) -> Event:
        ev = Event(kind="human_input", source="human", payload={"correction": correction}, mission_ids=[mission_id], trusted=True)
        self.events.emit(ev)
        self._reactivate(mission_id, f"human correction {ev.id}")
        return ev

    def inform(self, mission_id: str, information: str) -> Event:
        ev = Event(kind="human_input", source="human", payload={"new_information": information}, mission_ids=[mission_id], trusted=True)
        self.events.emit(ev)
        self._reactivate(mission_id, f"new information {ev.id}")
        return ev

    def emit_event(self, kind: str, payload: dict[str, Any], mission_ids: Optional[list[str]] = None, source: str = "external") -> Event:
        ev = Event(kind=kind, source=source, payload=payload, mission_ids=list(mission_ids or []), trusted=source in ("human", "system"))
        self.events.emit(ev)
        for mid in ev.routed_to:
            self._reactivate(mid, f"event {ev.kind} {ev.id}")
        return ev

    def _reactivate(self, mission_id: str, why: str) -> None:
        state = self.store.load_mission(mission_id)
        if state and state.status in (MissionStatus.BLOCKED_EXTERNAL, MissionStatus.PAUSED):
            state.status = MissionStatus.ACTIVE
            state.notes.append(f"reactivated: {why}")
            self.store.save_mission(state, "reactivated", {"why": why})

    # -- observation ------------------------------------------------------------------------

    def status(self, mission_id: Optional[str] = None) -> dict[str, Any]:
        mid = mission_id or self.store.kv_get("last_mission_id")
        state = self.store.load_mission(mid) if mid else None
        if state is None:
            return {"error": f"mission not found: {mid}"}
        planner = Planner(state)
        planner.compute_ready()
        return {
            "mission_id": state.mission_id,
            "status": state.status.value,
            "objective": state.objective,
            "executive_model": state.executive_model,
            "progress": state.progress,
            "confidence": state.confidence,
            "criteria": [{"id": c.id, "description": c.description, "satisfied": c.satisfied, "verifications": len(c.verification_ids)} for c in state.success_criteria],
            "tasks": {s.value: sum(1 for t in state.tasks if t.status == s) for s in TaskStatus},
            "ready_tasks": [t.title for t in planner.next_tasks(5)],
            "open_unknowns": [u.question for u in state.open_unknowns()[:5]],
            "claims": len(state.claims),
            "evidence": len(state.evidence),
            "contradictions_unresolved": len(state.unresolved_contradictions()),
            "blocked_operations": [f"{b.operation}: {b.reason} (unblock: {b.what_would_unblock})" for b in state.blocked_operations if not b.resolved],
            "human_requests": [{"id": h.id, "kind": h.kind, "question": h.question, "options": h.options} for h in state.unanswered_human_requests()],
            "usage": state.usage.model_dump(),
            "synthesis": {k: state.synthesis.get(k) for k in ("conclusion", "decision", "confidence", "mission_status") if k in state.synthesis},
            "notes": state.notes[-5:],
            "last_checkpoint_at": state.timestamps.last_checkpoint_at,
        }

    def workspace_text(self, mission_id: str) -> str:
        state = self.store.load_mission(mission_id)
        if state is None:
            return ""
        from cogos.beliefs import BeliefGraph
        from cogos.world_model import WorldModelManager

        planner = Planner(state)
        return GlobalWorkspace(self.config.workspace_max_chars).build(state, ready_tasks=planner.next_tasks(6), belief_lines=BeliefGraph(state).summary_for_workspace(8), world_lines=WorldModelManager(state.world_model).snapshot_summary(8)).text

    def explain(self, mission_id: str) -> dict[str, Any]:
        return self.tracer.explain(mission_id)

    # -- skills ---------------------------------------------------------------------------------

    def try_compile_skill(self, mission_id: str) -> Optional[dict[str, Any]]:
        state = self.store.load_mission(mission_id)
        if state is None or state.status != MissionStatus.COMPLETE:
            return None
        traces = self.store.traces(mission_id, limit=5000)
        cand = self.skills.propose_from_trajectory(state, traces)
        if cand is None:
            return None
        self.store.save_mission(state, "skill_candidate", {"candidate": cand.name})
        self.tracer.set_mission(mission_id)
        self.tracer.emit("learn", f"candidate skill proposed: {cand.name} (not promoted until evaluated)", data={"candidate_id": cand.id, "steps": len(cand.procedure)})
        return {"candidate_id": cand.id, "name": cand.name, "status": cand.status}

    # -- boot / recovery ------------------------------------------------------------------------

    def boot(self, brief: bool = False) -> BootReport:
        root = Path(self.config.repo_root)
        report = BootReport(repo_root=str(root), cogos_home=str(self.config.home), store_health=self.store.health())
        try:
            report.git_status = subprocess.run(["git", "status", "--short", "-b"], cwd=str(root), capture_output=True, text=True, encoding="utf-8", timeout=20, check=False).stdout[:1500]
            report.git_log = subprocess.run(["git", "log", "--oneline", "-5"], cwd=str(root), capture_output=True, text=True, encoding="utf-8", timeout=20, check=False).stdout[:800]
        except (OSError, subprocess.SubprocessError) as exc:
            report.warnings.append(f"git unavailable: {exc}")
        report.environment = {"executive_model": self.config.executive.model, "adapter": self.config.executive.adapter, "tools": [s.name for s in self.fabric.specs()], "unavailable_tools": [f"{s.name}: {s.unavailable_reason}" for s in self.fabric.specs(include_unavailable=True) if not s.available]}
        if self.config.executive.adapter == "claude_code":
            from cogos.adapters.claude_code import ClaudeCodeExecutive

            ok, why = ClaudeCodeExecutive(self.config.executive.model, binary=self.config.executive.claude_binary).available()
            report.environment["claude_binary"] = why
            if not ok:
                report.warnings.append(f"executive adapter unavailable: {why} (mission state preserved; no downgrade)")
        for row in self.store.list_missions():
            report.missions.append({k: row[k] for k in ("mission_id", "status", "objective", "updated_at")})
        target, reason = self.resume_target()
        report.resume_target, report.resume_reason = target, reason
        report.pending_events = [f"{e.kind} -> {e.routed_to}" for e in self.store.pending_events()[:10]]
        if target:
            state = self.store.load_mission(target)
            if state is not None:
                report.unresolved_tasks = [f"{t.status.value}: {t.title}" for t in state.tasks if t.status not in (TaskStatus.DONE, TaskStatus.CANCELLED)][:12]
                report.latest_verification = [f"{t.status.value}: {t.name} — {t.summary[:80]}" for t in state.tests[-5:]]
                report.human_requests = [f"{h.id}: {h.question[:120]}" for h in state.unanswered_human_requests()]
                if not brief:
                    report.workspace = self.workspace_text(target)
        if report.store_health.get("integrity") != "ok":
            report.warnings.append(f"store integrity: {report.store_health.get('integrity')}")
        return report

    def close(self) -> None:
        self.store.close()


def boot_summary(report: BootReport) -> str:
    lines = [f"cogos boot @ {report.booted_at}", f"repo: {report.repo_root}  home: {report.cogos_home}", f"store: schema v{report.store_health.get('schema_version')} integrity={report.store_health.get('integrity')} missions={report.store_health.get('counts', {}).get('missions')}", f"executive: {report.environment.get('executive_model')} via {report.environment.get('adapter')}"]
    if report.environment.get("unavailable_tools"):
        lines.append("unavailable tools: " + "; ".join(report.environment["unavailable_tools"]))
    for m in report.missions[:8]:
        lines.append(f"  mission {m['mission_id']} [{m['status']}] {m['objective'][:80]}")
    lines.append(f"resume: {report.resume_target or '-'} ({report.resume_reason})")
    if report.unresolved_tasks:
        lines.append("unresolved tasks: " + "; ".join(report.unresolved_tasks[:6]))
    if report.latest_verification:
        lines.append("latest verification: " + "; ".join(report.latest_verification[:3]))
    if report.human_requests:
        lines.append("awaiting human: " + "; ".join(report.human_requests[:3]))
    if report.pending_events:
        lines.append("pending events: " + "; ".join(report.pending_events[:5]))
    if report.git_status:
        lines.append("git: " + report.git_status.splitlines()[0] + (f" (+{len(report.git_status.splitlines()) - 1} changed)" if len(report.git_status.splitlines()) > 1 else ""))
    for w in report.warnings:
        lines.append("WARNING: " + w)
    return "\n".join(lines)


__all__ = ["Runtime", "BootReport", "boot_summary", "json"]
