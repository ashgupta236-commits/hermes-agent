"""Dynamic Agent Foundry.

Creates ephemeral specialist cognitive processes on demand. There is no fixed
society of agents: a specialist is a role prompt + a bounded slice of world
state + a tool surface + a structured output contract + a termination
criterion. Specialists inherit the resident executive model unless cheaper
routing was explicitly authorised in configuration.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import BaseModel, Field

from cogos.adapters.base import CognitionRequest, CognitionResponse, ExecutiveModel, UntrustedBlock
from cogos.adapters.schema_utils import schema_for
from cogos.config import CogosConfig
from cogos.prompts import PROMPTS
from cogos.governance.immune import scan_for_injection
from cogos.schemas.cognition import Disagreement, SpecialistReport, SpecialistSpec
from cogos.schemas.mission import MissionState

# Map runtime tool names to Claude Code built-in tools for headless specialists.
CLAUDE_TOOL_MAP: dict[str, str] = {
    "read_file": "Read",
    "read_document": "Read",
    "list_dir": "Glob",
    "search_text": "Grep",
    "write_file": "Write",
    "edit_file": "Edit",
    "shell": "Bash",
    "run_tests": "Bash",
    "git": "Bash",
    "web_fetch": "WebFetch",
    "web_search": "WebSearch",
    "calculate": "Bash",
}

ROLE_GUIDANCE: dict[str, str] = {
    "researcher": "Find primary sources first (official statistics, regulators, filings, papers). Record source, date, scope, and whether a source repeats another. Quantify where possible.",
    "scientist": "State the hypothesis, the discriminating observation, and the result. Separate measurement from interpretation.",
    "engineer": "Inspect the codebase before changing it. Follow existing conventions. Add or update tests for every behaviour change. Run the tests and report their actual output.",
    "debugger": "Reproduce first. Form at least two hypotheses, design an experiment that discriminates them, and fix the cause, not the symptom. Run tests before and after.",
    "statistician": "Check sample sizes, base rates, and confounders. Prefer computed statistics over narrative claims; show the calculation.",
    "economist": "Model incentives, costs, and second-order effects. Distinguish nominal from real values and stock from flow.",
    "market_analyst": "Size the market bottom-up and top-down; reconcile the two. Identify the binding constraint on entry.",
    "architect": "Compare at least two designs on maintainability, testability, and blast radius. Recommend one with rationale.",
    "security_reviewer": "Enumerate trust boundaries, inputs, and privilege. Report concrete exploitable issues with severity; do not pad with generic advice.",
    "skeptic": "Your job is to find what is wrong. Attack assumptions, look for disconfirming evidence, and identify the cheapest observation that would falsify the leading position.",
    "forecaster": "Give calibrated probabilities with base rates and the key drivers. State what would move the estimate.",
    "planner": "Decompose into a dependency-ordered DAG with explicit verification steps.",
    "verifier": "Independently check the artifact against its requirement using deterministic checks where available. Report exactly what was checked.",
    "source_auditor": "For every cited source: is it primary? independent? current? does it actually support the exact proposition claimed?",
    "causal_analyst": "Separate correlation from mechanism. Propose interventions and what each would reveal.",
    "mathematical_analyst": "Derive rather than assert. Verify numerically with a calculation.",
    "requirements_critic": "Find ambiguities, contradictions, and untestable requirements; propose testable rewrites.",
}


class DisagreementReport(BaseModel):
    disagreements: list[Disagreement] = Field(default_factory=list)
    summary: str = ""


class SpawnDecision(BaseModel):
    spawn: bool
    reason: str


@dataclass
class SpecialistRun:
    spec: SpecialistSpec
    report: Optional[SpecialistReport]
    response: CognitionResponse
    injection_flags: list[str] = field(default_factory=list)
    model: str = ""


class AgentFoundry:
    def __init__(self, adapter: ExecutiveModel, config: CogosConfig, repo_root: Optional[str] = None):
        self.adapter = adapter
        self.config = config
        self.repo_root = repo_root or str(config.repo_root)
        self.spawned: list[SpecialistRun] = []

    # -- policy ----------------------------------------------------------------------

    def specialist_model(self) -> str:
        ex = self.config.executive
        if ex.allow_cheaper_specialist_models and ex.specialist_model:
            return ex.specialist_model
        return ex.model

    @staticmethod
    def should_spawn(spec: SpecialistSpec, *, parallel_value: bool = False, isolation_value: bool = False, expertise_value: bool = True, trivial: bool = False, budget_left: int = 1) -> SpawnDecision:
        if budget_left <= 0:
            return SpawnDecision(spawn=False, reason="subagent budget exhausted")
        if trivial:
            return SpawnDecision(spawn=False, reason="trivial operation; a direct tool call or single reasoning step is faster")
        if spec.independent:
            return SpawnDecision(spawn=True, reason="independent reasoning requested")
        if parallel_value or isolation_value or expertise_value:
            why = [n for n, v in (("parallel work", parallel_value), ("context isolation", isolation_value), ("specialist perspective", expertise_value)) if v]
            return SpawnDecision(spawn=True, reason=", ".join(why))
        return SpawnDecision(spawn=False, reason="no parallel, isolation, or expertise value")

    # -- context slicing ---------------------------------------------------------------

    @staticmethod
    def context_slice(state: MissionState, keys: list[str], *, withhold_conclusions: bool = False, max_items: int = 15) -> dict[str, Any]:
        ctx: dict[str, Any] = {"objective": state.objective, "constraints": state.explicit_constraints + state.inferred_constraints}
        if "claims" in keys and not withhold_conclusions:
            top = sorted(state.claims, key=lambda c: -c.decision_relevance)[:max_items]
            ctx["claims"] = [{"id": c.id, "proposition": c.proposition, "status": c.status.value, "confidence": c.confidence, "epistemic_status": c.epistemic_status.value} for c in top]
        if "evidence" in keys:
            ctx["evidence"] = [{"id": e.id, "summary": e.summary, "source": e.provenance.source, "kind": e.kind.value, "scope": e.scope, "freshness": e.freshness} for e in state.evidence[-max_items:]]
        if "unknowns" in keys:
            ctx["unknowns"] = [{"id": u.id, "question": u.question} for u in state.open_unknowns()[:max_items]]
        if "hypotheses" in keys and not withhold_conclusions:
            ctx["hypotheses"] = [{"id": h.id, "statement": h.statement, "status": h.status, "confidence": h.confidence} for h in state.hypotheses[:max_items]]
        if "tasks" in keys:
            ctx["tasks"] = [{"id": t.id, "title": t.title, "status": t.status.value} for t in state.tasks[:max_items]]
        if "world" in keys:
            ctx["entities"] = [{"name": e.name, "kind": e.kind, "properties": [{p.name: p.value, "status": p.epistemic_status.value} for p in e.properties[:6]]} for e in state.world_model.entities[:max_items]]
        if "synthesis" in keys and not withhold_conclusions and state.synthesis:
            ctx["current_synthesis"] = state.synthesis
        if "facts" in keys:
            ctx["known_facts"] = [f.statement for f in state.known_facts[:max_items]]
        return ctx

    # -- request construction ---------------------------------------------------------------

    def build_request(self, spec: SpecialistSpec, state: MissionState, *, untrusted: Optional[list[UntrustedBlock]] = None, mission_id: Optional[str] = None) -> CognitionRequest:
        withhold = spec.independent
        ctx = self.context_slice(state, spec.context_keys or ["evidence", "unknowns"], withhold_conclusions=withhold)
        guidance = ROLE_GUIDANCE.get(spec.role.lower().replace(" ", "_"), "Apply rigorous, evidence-first reasoning appropriate to the role.")
        tools = [CLAUDE_TOOL_MAP[t] for t in spec.tools if t in CLAUDE_TOOL_MAP]
        tools = sorted(set(tools))
        allowed_patterns: list[str] = []
        if "Bash" in tools:
            allowed_patterns += ["Bash(python*)", "Bash(pytest*)", "Bash(git status*)", "Bash(git diff*)", "Bash(git log*)", "Bash(ls*)", "Bash(cat*)", "Bash(rg*)", "Bash(grep*)", "Bash(find*)", "Bash(uv run*)", "Bash(npm test*)", "Bash(make test*)"]
        prompt_parts = [
            f"ROLE: {spec.role}",
            f"ROLE GUIDANCE: {guidance}",
            f"OBJECTIVE: {spec.objective}",
            "CONSTRAINTS: " + ("; ".join(spec.constraints) if spec.constraints else "none beyond the constitution"),
            f"EVIDENCE STANDARD: {spec.evidence_standard}",
            f"TERMINATION: {spec.termination_criterion}",
            "PERMITTED TOOLS: " + (", ".join(tools) if tools else "none (reasoning only)"),
            "WORKING DIRECTORY: " + self.repo_root,
            "CONTEXT (structured state slice; executive conclusions withheld for independence)" if withhold else "CONTEXT (structured state slice)",
            json.dumps(ctx, indent=1, default=str)[:20_000],
            "Return the SpecialistReport JSON. Every finding must carry evidence with a source.",
        ]
        return CognitionRequest(
            kind="specialist",
            system_prompt=PROMPTS["specialist"],
            prompt="\n".join(prompt_parts),
            schema_name="SpecialistReport",
            output_schema=schema_for(SpecialistReport),
            model=self.specialist_model(),
            untrusted=list(untrusted or []),
            tools=tools,
            allowed_tool_patterns=allowed_patterns,
            max_turns=max(1, spec.max_turns),
            cwd=self.repo_root if tools else None,
            effort=self.config.executive.effort,
            timeout_seconds=self.config.executive.call_timeout_seconds,
            mission_id=mission_id,
            metadata={"spec": spec.model_dump(mode="json"), "context": ctx, "requires_model": True},
        )

    # -- execution --------------------------------------------------------------------------------

    def run(self, spec: SpecialistSpec, state: MissionState, *, untrusted: Optional[list[UntrustedBlock]] = None) -> SpecialistRun:
        req = self.build_request(spec, state, untrusted=untrusted, mission_id=state.mission_id)
        resp = self.adapter.call(req)
        report: Optional[SpecialistReport] = None
        flags: list[str] = []
        if resp.ok:
            try:
                report = SpecialistReport.model_validate(resp.parsed)
            except Exception as exc:  # noqa: BLE001 - validation failure is a structured outcome
                resp = resp.model_copy(update={"ok": False, "error": f"invalid specialist report: {exc}", "error_kind": "schema"})
            if report is not None:
                text = report.conclusion + " " + report.raw_notes + " ".join(f.statement for f in report.findings)
                flags = scan_for_injection(text)
        run = SpecialistRun(spec=spec, report=report, response=resp, injection_flags=flags, model=req.model)
        self.spawned.append(run)
        return run

    def run_parallel(self, specs: list[SpecialistSpec], state: MissionState, max_workers: int = 4) -> list[SpecialistRun]:
        if len(specs) <= 1:
            return [self.run(s, state) for s in specs]
        with ThreadPoolExecutor(max_workers=min(max_workers, len(specs))) as pool:
            return list(pool.map(lambda s: self.run(s, state), specs))

    # -- independent cognition protocol ---------------------------------------------------------

    def independent_challenge(self, question: str, state: MissionState, executive_position: str, *, tools: Optional[list[str]] = None, max_turns: int = 10) -> tuple[SpecialistRun, DisagreementReport]:
        """Run an independent solver that never sees the executive's conclusion, then extract disagreements."""
        spec = SpecialistSpec(
            role="skeptic",
            objective=f"Independently answer: {question}",
            independent=True,
            context_keys=["evidence", "unknowns", "facts"],
            tools=list(tools or []),
            max_turns=max_turns,
            evidence_standard="cite provenance for every finding; state confidence and what would change it",
        )
        run = self.run(spec, state)
        specialist_position = run.report.conclusion if run.report else "(no independent conclusion produced)"
        req = CognitionRequest(
            kind="challenge",
            system_prompt=PROMPTS["challenge"],
            prompt=f"QUESTION: {question}\n\nPOSITION A (executive): {executive_position}\n\nPOSITION B (independent specialist): {specialist_position}\n\nFindings B: {json.dumps([f.model_dump(mode='json') for f in (run.report.findings if run.report else [])], default=str)[:8000]}",
            schema_name="DisagreementReport",
            output_schema=schema_for(DisagreementReport),
            model=self.config.executive.model,
            mission_id=state.mission_id,
            metadata={"question": question, "executive_position": executive_position, "specialist_position": specialist_position},
        )
        resp = self.adapter.call(req)
        report = DisagreementReport()
        if resp.ok:
            try:
                report = DisagreementReport.model_validate(resp.parsed)
            except Exception:  # noqa: BLE001
                report = DisagreementReport(summary="unparseable disagreement report")
        return run, report
