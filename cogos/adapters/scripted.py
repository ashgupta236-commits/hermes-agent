"""Deterministic executive policies.

``HeuristicExecutive`` is a general rule-based executive that can drive the
runtime end to end without a model: it compiles objectives into professional
default plans, selects the next step by expected value over the task DAG,
interprets tool results, and synthesises from verified state. It is used for
offline demos, evaluations, and as the fallback policy of
``ScriptedExecutive`` which lets tests inject specific structured responses.

Neither policy is a mock of a particular demonstration: the rules are generic
and operate on the structured ``metadata`` the runtime attaches to each request.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

from cogos.adapters.base import CognitionRequest, CognitionResponse
from cogos.schemas.cognition import (
    ClaimSpec,
    CriterionSpec,
    EvidenceSpec,
    GoalSpec,
    HypothesisSpec,
    MissionCompilation,
    ObservationInterpretation,
    RiskSpec,
    SpecialistReport,
    SpecialistSpec,
    StepDecision,
    Synthesis,
    TaskSpec,
    TaskUpdateSpec,
    ToolCallSpec,
    UnknownSpec,
    VerificationJudgment,
)
from cogos.schemas.common import EpistemicStatus, OperationKind

Policy = Callable[[CognitionRequest], dict[str, Any]]


def _j(obj: Any) -> str:
    return json.dumps(obj, default=str)


# ------------------------------------------------------------------------------
# Mission-kind detection and default plans
# ------------------------------------------------------------------------------

_IMPL = re.compile(r"\b(build|implement|create|add|develop|write|refactor|port|ship|complete (the|this) (project|feature))\b", re.I)
_REPAIR = re.compile(r"\b(fix|debug|why .* (fail|broken|slow)|failing|broken|not working|regression|error)\b", re.I)
_RESEARCH = re.compile(r"\b(research|investigate|whether|should|evaluate|assess|compare|determine|analy[sz]e|find out|decide|is it (worth|true))\b", re.I)


def detect_mission_kind(objective: str) -> str:
    o = objective.strip()
    if _REPAIR.search(o) and not _IMPL.search(o):
        return "repair"
    if _IMPL.search(o) and not re.search(r"\b(whether|should)\b", o, re.I):
        return "implementation"
    if _RESEARCH.search(o):
        return "decision" if re.search(r"\b(should|whether|decide|launch|enter)\b", o, re.I) else "research"
    return "general"


def _topic(objective: str) -> str:
    t = re.sub(r"^(please\s+)?(research|investigate|determine|evaluate|assess|analy[sz]e|find out|decide)\s+(whether|if|why|how|what)?\s*", "", objective.strip(), flags=re.I)
    return t.rstrip(". ") or objective


def default_compilation(objective: str, context: dict[str, Any]) -> MissionCompilation:
    kind = detect_mission_kind(objective)
    topic = _topic(objective)
    has_req = bool(context.get("has_requirements"))
    req_path = context.get("requirements_path") or "REQUIREMENTS.md"
    if kind == "implementation":
        return MissionCompilation(
            interpretation=f"Implement: {topic}. Inspect the repository and requirements, choose an architecture, implement, test, and verify.",
            mission_kind="implementation",
            success_criteria=[
                CriterionSpec(description="The feature is implemented as described in the requirements", verification_method="tests pass for the feature and code review of the diff", explicit=True),
                CriterionSpec(description="The full test suite passes", verification_method="pytest exits 0", explicit=False),
            ],
            explicit_constraints=[],
            inferred_constraints=["Follow existing repository conventions", "Do not weaken or delete legitimate tests", "Keep changes minimal and reversible"],
            assumptions=["The requirements document is the authoritative specification"],
            unknowns=[UnknownSpec(question="What exactly do the requirements demand and which existing modules are affected?", decision_importance=0.9, probability_changes_decision=0.8, expected_information_gain=0.9, estimated_cost=0.1)],
            goals=[GoalSpec(key="g1", title="Understand requirements and codebase"), GoalSpec(key="g2", title="Implement and test the feature"), GoalSpec(key="g3", title="Verify and finish")],
            tasks=[
                TaskSpec(key="inspect", title="Inspect repository layout", goal_key="g1", operation_hint="inspect_files", parameters_json=_j({"tool": "list_dir", "arguments": {"path": ".", "glob": "*"}}), priority=0.9, resolves_unknowns=["What exactly do the requirements demand"]),
                TaskSpec(key="reqs", title="Read the requirements", goal_key="g1", operation_hint="inspect_files", parameters_json=_j({"tool": "read_file", "arguments": {"path": req_path}}), priority=0.95 if has_req else 0.6, resolves_unknowns=["What exactly do the requirements demand"]),
                TaskSpec(key="arch", title="Choose architecture and implementation plan", goal_key="g2", depends_on=["inspect", "reqs"], operation_hint="direct_reasoning", priority=0.7, addresses_criteria=["feature is implemented"]),
                TaskSpec(key="impl", title="Implement the feature", goal_key="g2", depends_on=["arch"], operation_hint="instantiate_specialist", parameters_json=_j({"role": "engineer", "objective": f"Implement in this repository: {topic}. Follow the requirements exactly, add tests, keep the diff minimal.", "tools": ["read_file", "list_dir", "search_text", "write_file", "shell", "run_tests"], "max_turns": 40}), priority=0.8, parallel_safe=False, addresses_criteria=["feature is implemented"]),
                TaskSpec(key="tests", title="Run the test suite", goal_key="g2", depends_on=["impl"], operation_hint="verify", parameters_json=_j({"commands": [context.get("test_command") or "python -m pytest -q"]}), priority=0.85, parallel_safe=False, addresses_criteria=["full test suite passes", "feature is implemented"]),
                TaskSpec(key="synth", title="Summarise implementation and evidence", goal_key="g3", depends_on=["tests"], operation_hint="synthesize", priority=0.6),
            ],
            risks=[RiskSpec(description="Requirements ambiguous; chosen defaults may not match intent", probability=0.3, impact=0.5, mitigation="Record assumptions explicitly in the decision journal")],
            required_tests=[context.get("test_command") or "python -m pytest -q"],
        )
    if kind == "repair":
        return MissionCompilation(
            interpretation=f"Diagnose and fix: {topic}. Reproduce, form competing hypotheses, run discriminating experiments, fix, and verify.",
            mission_kind="repair",
            success_criteria=[
                CriterionSpec(description="Root cause identified with evidence", verification_method="evidence: a discriminating experiment supports one hypothesis and refutes the alternatives"),
                CriterionSpec(description="The failure no longer reproduces and tests pass", verification_method="tests pass (pytest exits 0)"),
            ],
            inferred_constraints=["Do not mask the symptom; fix the cause", "Preserve passing behaviour"],
            unknowns=[UnknownSpec(question="What is the root cause of the failure?", decision_importance=1.0, probability_changes_decision=0.9, expected_information_gain=0.9, estimated_cost=0.3)],
            hypotheses=[
                HypothesisSpec(question="What is the root cause?", statement="A recent code change introduced a regression", prior=0.4, unique_predictions=["The failure appears only after the change; reverting it removes the failure"]),
                HypothesisSpec(question="What is the root cause?", statement="An environmental or dependency difference causes the failure", prior=0.3, unique_predictions=["The failure depends on environment; it does not reproduce in a clean environment"]),
                HypothesisSpec(question="What is the root cause?", statement="The failing expectation itself is wrong or stale", prior=0.3, unique_predictions=["The behaviour matches the specification and the test contradicts the spec"]),
            ],
            goals=[GoalSpec(key="g1", title="Reproduce and localise"), GoalSpec(key="g2", title="Discriminate hypotheses"), GoalSpec(key="g3", title="Fix and verify")],
            tasks=[
                TaskSpec(key="repro", title="Reproduce the failure", goal_key="g1", operation_hint="verify", parameters_json=_j({"commands": [context.get("test_command") or "python -m pytest -q"], "expect_failure": True}), priority=0.95, resolves_unknowns=["root cause"]),
                TaskSpec(key="inspect", title="Inspect recent changes", goal_key="g1", operation_hint="inspect_files", parameters_json=_j({"tool": "git", "arguments": {"args": ["log", "--oneline", "-15"]}}), priority=0.8),
                TaskSpec(key="experiment", title="Run discriminating experiment for the leading hypotheses", goal_key="g2", depends_on=["repro", "inspect"], operation_hint="falsify", priority=0.9, resolves_unknowns=["root cause"]),
                TaskSpec(key="fix", title="Implement the fix", goal_key="g3", depends_on=["experiment"], operation_hint="instantiate_specialist", parameters_json=_j({"role": "debugger", "objective": f"Fix the root cause of: {topic}", "tools": ["read_file", "search_text", "write_file", "shell", "run_tests"], "max_turns": 40}), priority=0.85, parallel_safe=False),
                TaskSpec(key="tests", title="Verify the fix with the test suite", goal_key="g3", depends_on=["fix"], operation_hint="verify", parameters_json=_j({"commands": [context.get("test_command") or "python -m pytest -q"]}), priority=0.9, addresses_criteria=["no longer reproduces"]),
                TaskSpec(key="synth", title="Summarise root cause and fix", goal_key="g3", depends_on=["tests"], operation_hint="synthesize", priority=0.6),
            ],
            required_tests=[context.get("test_command") or "python -m pytest -q"],
        )
    # research / decision / general
    decision = kind == "decision"
    unknown_specs = [
        UnknownSpec(question=f"What is the size and nature of the demand/opportunity for {topic}?", decision_importance=0.9, probability_changes_decision=0.7, expected_information_gain=0.8, estimated_cost=0.4),
        UnknownSpec(question=f"What regulatory, legal, or structural constraints apply to {topic}?", decision_importance=0.9, probability_changes_decision=0.8, expected_information_gain=0.8, estimated_cost=0.4),
        UnknownSpec(question=f"What are the costs, required investment, and timeline for {topic}?", decision_importance=0.8, probability_changes_decision=0.6, expected_information_gain=0.7, estimated_cost=0.4),
        UnknownSpec(question=f"Who are the competitors/alternatives and what is the evidence on their performance regarding {topic}?", decision_importance=0.7, probability_changes_decision=0.5, expected_information_gain=0.6, estimated_cost=0.4),
        UnknownSpec(question=f"What are the major risks and failure modes for {topic}?", decision_importance=0.8, probability_changes_decision=0.6, expected_information_gain=0.6, estimated_cost=0.3),
    ] if decision else [
        UnknownSpec(question=f"What is the current state of primary evidence on {topic}?", decision_importance=0.9, probability_changes_decision=0.7, expected_information_gain=0.9, estimated_cost=0.4),
        UnknownSpec(question=f"What are the strongest counter-arguments or contradictory findings on {topic}?", decision_importance=0.8, probability_changes_decision=0.7, expected_information_gain=0.7, estimated_cost=0.4),
        UnknownSpec(question=f"Which sources are primary and independent for {topic}?", decision_importance=0.6, probability_changes_decision=0.4, expected_information_gain=0.6, estimated_cost=0.2),
    ]
    hyps = [
        HypothesisSpec(question=objective, statement=f"Yes: {topic} is favourable on balance", prior=0.5, unique_predictions=["Demand and constraints evidence net positive; expected value positive under base assumptions"]),
        HypothesisSpec(question=objective, statement=f"No: {topic} is unfavourable on balance", prior=0.5, unique_predictions=["A binding constraint, cost, or risk dominates the upside"]),
    ] if decision else []
    research_tasks = [
        TaskSpec(key=f"u{i}", title=f"Investigate: {u.question[:90]}", goal_key="g2", depends_on=["frame"], operation_hint="instantiate_specialist", parameters_json=_j({"role": "researcher", "objective": u.question, "tools": ["web_search", "web_fetch", "read_file", "search_text"], "max_turns": 15, "independent": False}), priority=0.6 + 0.3 * u.decision_importance, resolves_unknowns=[u.question])
        for i, u in enumerate(unknown_specs)
    ]
    tasks = [
        TaskSpec(key="context", title="Inspect local context and provided documents", goal_key="g1", operation_hint="inspect_files", parameters_json=_j({"tool": "list_dir", "arguments": {"path": ".", "glob": "*"}}), priority=0.7),
        TaskSpec(key="frame", title="Frame the question, criteria, and decision-relevant unknowns", goal_key="g1", depends_on=["context"], operation_hint="direct_reasoning", priority=0.8),
        *research_tasks,
        TaskSpec(key="evaluate", title="Evaluate alternatives and model expected outcomes", goal_key="g3", depends_on=[t.key for t in research_tasks], operation_hint="simulate" if decision else "direct_reasoning", priority=0.8),
        TaskSpec(key="challenge", title="Independent challenge of the leading conclusion", goal_key="g3", depends_on=["evaluate"], operation_hint="instantiate_specialist", parameters_json=_j({"role": "skeptic", "objective": f"Independently assess: {objective}", "independent": True, "tools": ["web_search", "web_fetch"], "max_turns": 12}), priority=0.75),
        TaskSpec(key="verify", title="Verify sources, independence, and freshness of key claims", goal_key="g3", depends_on=["challenge"], operation_hint="verify", parameters_json=_j({"research": True}), priority=0.85),
        TaskSpec(key="synth", title="Synthesise the conclusion", goal_key="g3", depends_on=["verify"], operation_hint="synthesize", priority=0.9),
    ]
    return MissionCompilation(
        interpretation=f"{'Decide' if decision else 'Research'}: {topic}. Acquire decision-relevant evidence, evaluate alternatives, challenge, verify, synthesise.",
        mission_kind=kind,
        success_criteria=[
            CriterionSpec(description=f"A {'decision' if decision else 'conclusion'} on '{topic}' with rationale grounded in verified evidence", verification_method="evidence: key claims supported by independent primary sources with no unresolved material contradiction"),
            CriterionSpec(description="Remaining uncertainty is explicitly bounded and cannot reasonably change the conclusion", verification_method="evidence: open unknowns have decision-changing probability below threshold or are documented as bounded"),
        ],
        inferred_constraints=["Prefer primary, recent, independent sources", "Keep contradictions visible until resolved or bounded"],
        unknowns=unknown_specs,
        hypotheses=hyps,
        goals=[GoalSpec(key="g1", title="Frame the question"), GoalSpec(key="g2", title="Acquire decision-relevant evidence"), GoalSpec(key="g3", title="Evaluate, challenge, verify, conclude")],
        tasks=tasks,
        risks=[RiskSpec(description="False consensus from sources repeating one report", probability=0.4, impact=0.6, mitigation="Track lineage; count independent roots")],
    )


# ------------------------------------------------------------------------------
# Heuristic executive
# ------------------------------------------------------------------------------


class HeuristicExecutive:
    """Rule-based executive: general policies over structured request metadata."""

    name = "heuristic"

    def __init__(self, model: str = "heuristic", **_: Any):
        self.model = model
        self.calls: list[CognitionRequest] = []

    def call(self, req: CognitionRequest) -> CognitionResponse:
        self.calls.append(req)
        handler = getattr(self, f"_{req.kind}", None)
        if handler is None:
            return CognitionResponse(ok=False, model_requested=req.model, error=f"unsupported cognition kind {req.kind}", error_kind="structural")
        parsed = handler(req)
        return CognitionResponse(ok=True, parsed=parsed, model_requested=req.model, models_used=[self.model], turns=1, duration_ms=1)

    # compile ---------------------------------------------------------------------

    def _compile(self, req: CognitionRequest) -> dict[str, Any]:
        md = req.metadata
        comp = default_compilation(str(md.get("objective", "")), dict(md.get("context") or {}))
        return comp.model_dump(mode="json")

    # select ----------------------------------------------------------------------

    def _select(self, req: CognitionRequest) -> dict[str, Any]:
        md = req.metadata
        directives = list(md.get("directives") or [])
        human = [h for h in md.get("human_requests") or [] if not h.get("answered")]
        if human and not md.get("independent_work_remaining", True):
            return StepDecision(operation=OperationKind.REQUEST_HUMAN_AUTHORIZATION, rationale="A non-inferable external decision blocks all remaining work", human_request=None, confidence=0.9).model_dump(mode="json")
        if any(d.startswith("must_falsify") for d in directives) and md.get("falsification_target"):
            tgt = md["falsification_target"]
            return StepDecision(operation=OperationKind.FALSIFY, task_id=str(md.get("falsify_task_id", "")), rationale=f"Serious contradiction/high-stakes claim requires targeted falsification: {tgt.get('statement', '')[:120]}", specialists=[SpecialistSpec(role="skeptic", objective=f"Try to falsify: {tgt.get('statement', '')}. Conditions: {tgt.get('conditions', [])}", independent=True, tools=list(md.get("research_tools") or []), context_keys=["evidence", "claims"], max_turns=10)], confidence=0.7, consequential=True).model_dump(mode="json")
        pending_verify = list(md.get("verification_pending") or [])
        if pending_verify and any(d.startswith("must_verify") for d in directives):
            return StepDecision(operation=OperationKind.VERIFY, task_id=pending_verify[0], rationale="Completed work has unverified outputs; verification precedes claiming criteria", confidence=0.8).model_dump(mode="json")
        ready = list(md.get("ready_tasks") or [])
        if ready:
            task = ready[0]
            return self._decision_for_task(task, md).model_dump(mode="json")
        criteria = list(md.get("criteria") or [])
        all_sat = bool(criteria) and all(c.get("satisfied") for c in criteria)
        if all_sat:
            return StepDecision(operation=OperationKind.COMPLETE_MISSION, rationale="All success criteria are satisfied and verified", confidence=0.85).model_dump(mode="json")
        if pending_verify:
            return StepDecision(operation=OperationKind.VERIFY, task_id=pending_verify[0], rationale="Verify completed work before synthesis", confidence=0.75).model_dump(mode="json")
        if md.get("criteria_verification_pending"):
            return StepDecision(operation=OperationKind.VERIFY, task_id="", rationale="Check success criteria against verified state", confidence=0.7).model_dump(mode="json")
        if not md.get("synthesis_exists"):
            return StepDecision(operation=OperationKind.SYNTHESIZE, rationale="Plan exhausted; synthesise from verified state", confidence=0.7).model_dump(mode="json")
        if md.get("blocked_external"):
            return StepDecision(operation=OperationKind.WAIT_FOR_EXTERNAL_EVENT, rationale="Blocked on an external dependency; wait for the unblocking event", wait_for_event_kind=str(md.get("blocked_event_kind") or "human_input"), confidence=0.6).model_dump(mode="json")
        return StepDecision(operation=OperationKind.COMPLETE_MISSION, rationale="Attempt completion; the integrity gate will refuse if criteria are unmet", confidence=0.5).model_dump(mode="json")

    def _decision_for_task(self, task: dict[str, Any], md: dict[str, Any]) -> StepDecision:
        hint = task.get("operation_hint") or ""
        params = dict(task.get("parameters") or {})
        tid = task["id"]
        title = task.get("title", "")
        try:
            op = OperationKind(hint) if hint else OperationKind.DIRECT_REASONING
        except ValueError:
            op = OperationKind.DIRECT_REASONING
        rationale = f"Highest-value ready task: {title}"
        if op in (OperationKind.INSPECT_FILES, OperationKind.EXECUTE_CODE, OperationKind.USE_EXTERNAL_TOOL, OperationKind.EXECUTE_ACTION, OperationKind.SEARCH, OperationKind.RETRIEVE_MEMORY, OperationKind.RUN_EXPERIMENT):
            calls = []
            if params.get("tool"):
                calls.append(ToolCallSpec(tool=params["tool"], arguments_json=_j(params.get("arguments") or {}), purpose=title))
            for c in params.get("tool_calls") or []:
                calls.append(ToolCallSpec(tool=c.get("tool", ""), arguments_json=_j(c.get("arguments") or {}), purpose=c.get("purpose", title)))
            if not calls and op == OperationKind.RETRIEVE_MEMORY:
                calls.append(ToolCallSpec(tool="memory_search", arguments_json=_j({"query": title}), purpose=title))
            if not calls:
                calls.append(ToolCallSpec(tool="list_dir", arguments_json=_j({"path": ".", "glob": "*"}), purpose=title))
            return StepDecision(operation=op, task_id=tid, rationale=rationale, tool_calls=calls, confidence=0.7)
        if op == OperationKind.CALCULATE:
            return StepDecision(operation=op, task_id=tid, rationale=rationale, calculation=str(params.get("program") or params.get("expression") or "0"), confidence=0.8)
        if op == OperationKind.SIMULATE:
            return StepDecision(operation=op, task_id=tid, rationale=rationale, simulation_json=_j(params.get("scenario") or {}), confidence=0.6, consequential=True)
        if op in (OperationKind.INSTANTIATE_SPECIALIST, OperationKind.PARALLEL_WORKSTREAMS, OperationKind.FALSIFY):
            specs = params.get("specialists") or [params] if (params.get("role") or params.get("specialists")) else [{"role": "analyst", "objective": title}]
            spec_models = [
                SpecialistSpec(
                    role=str(s.get("role", "analyst")),
                    objective=str(s.get("objective", title)),
                    constraints=list(s.get("constraints") or []),
                    tools=list(s.get("tools") or []),
                    independent=bool(s.get("independent", False)),
                    max_turns=int(s.get("max_turns", 12)),
                    context_keys=list(s.get("context_keys") or ["claims", "evidence"]),
                )
                for s in specs
            ]
            return StepDecision(operation=op, task_id=tid, rationale=rationale, specialists=spec_models, confidence=0.65, consequential=op == OperationKind.FALSIFY)
        if op == OperationKind.VERIFY:
            return StepDecision(operation=op, task_id=tid, rationale=rationale, confidence=0.8)
        if op == OperationKind.SYNTHESIZE:
            return StepDecision(operation=op, task_id=tid, rationale=rationale, confidence=0.7, consequential=True)
        if op == OperationKind.COMPLETE_MISSION:
            return StepDecision(operation=op, task_id=tid, rationale=rationale, confidence=0.6)
        # direct reasoning: produce a structured conclusion from available state
        beliefs = md.get("belief_lines") or []
        unknowns = md.get("unknowns") or []
        reasoning = f"{title}: proceeding with professional defaults. Known beliefs: {len(beliefs)}. Open unknowns: {len(unknowns)}."
        return StepDecision(operation=OperationKind.DIRECT_REASONING, task_id=tid, rationale=rationale, reasoning_output=reasoning, confidence=0.6)

    # interpret ---------------------------------------------------------------------

    def _interpret(self, req: CognitionRequest) -> dict[str, Any]:
        md = req.metadata
        op = str(md.get("operation", ""))
        task_id = str(md.get("task_id") or "")
        interp = ObservationInterpretation(summary="", progress_estimate=float(md.get("progress", 0.0)), confidence=0.6)
        results = list(md.get("tool_results") or [])
        reports = list(md.get("specialist_reports") or [])
        ok_all = True
        pieces: list[str] = []
        for r in results:
            tool = r.get("tool", "tool")
            if r.get("ok"):
                pieces.append(f"{tool} ok")
                kind = "primary" if tool in ("run_tests", "calculate", "git", "shell") else "secondary"
                summary = f"{tool} output: {str(r.get('output', ''))[:200]}"
                interp.new_evidence.append(EvidenceSpec(summary=summary, source=f"tool:{tool}", kind=kind, reliability=0.9, excerpt=str(r.get("output", ""))[:500]))
                if r.get("injection_flags"):
                    interp.injection_detected = True
                    interp.lessons.append(f"Injection attempt detected in {tool} output: {r['injection_flags']}")
            else:
                ok_all = False
                pieces.append(f"{tool} failed: {r.get('error', '')[:120]}")
                ek = r.get("error_kind") or "structural"
                failure_kind = "transient" if ek == "transient" else ("tool" if ek in ("denied", "requires_human", "unavailable") else "structural")
                interp.failure_lessons.append(f"{tool} failed ({ek}): {r.get('error', '')[:160]}")
                if task_id:
                    interp.task_updates.append(TaskUpdateSpec(task_id=task_id, status="blocked" if ek in ("denied", "requires_human") else "failed", failure_reason=str(r.get("error", ""))[:300], failure_kind=failure_kind))
        for rep in reports:
            conf = float(rep.get("confidence", 0.5))
            pieces.append(f"specialist {rep.get('role')} concluded ({conf:.2f})")
            for f in rep.get("findings") or []:
                for ev in f.get("evidence") or []:
                    interp.new_evidence.append(EvidenceSpec(**{k: v for k, v in ev.items() if k in EvidenceSpec.model_fields}))
                if f.get("statement"):
                    interp.new_claims.append(ClaimSpec(proposition=str(f["statement"]), epistemic_status=EpistemicStatus(f.get("epistemic_status", "inference")), confidence=float(f.get("confidence", 0.5)), decision_relevance=0.6))
            if rep.get("blocked"):
                ok_all = False
                if task_id:
                    interp.task_updates.append(TaskUpdateSpec(task_id=task_id, status="blocked", failure_reason=str(rep.get("blocked_reason", ""))[:300], failure_kind="tool"))
            for u in rep.get("unresolved") or []:
                interp.new_unknowns.append(UnknownSpec(question=str(u)[:200], decision_importance=0.4, probability_changes_decision=0.3, expected_information_gain=0.4, estimated_cost=0.4))
        if op == "direct_reasoning" and md.get("reasoning_output"):
            interp.new_claims.append(ClaimSpec(proposition=str(md["reasoning_output"])[:300], epistemic_status=EpistemicStatus.INFERENCE, confidence=0.55, decision_relevance=0.5))
            pieces.append("reasoning recorded")
        if md.get("calculation_result") is not None:
            interp.new_evidence.append(EvidenceSpec(summary=f"Calculated: {md['calculation_result']}", source="calc:safe_eval", kind="primary", reliability=0.99))
            pieces.append("calculation done")
        if md.get("simulation_result"):
            sim = md["simulation_result"]
            interp.new_claims.append(ClaimSpec(proposition=f"Under stated assumptions the best option is '{sim.get('best_option')}' (margin {sim.get('margin')})", epistemic_status=EpistemicStatus.PREDICTION, confidence=0.6 if sim.get("robust_best") else 0.45, decision_relevance=0.9))
            pieces.append("simulation done")
        if md.get("verification"):
            ver = md["verification"]
            pieces.append(f"verification {ver.get('status')}")
            if ver.get("status") != "passed":
                ok_all = False
                if task_id:
                    interp.task_updates.append(TaskUpdateSpec(task_id=task_id, status="failed", failure_reason=str(ver.get("summary", ""))[:300], failure_kind="implementation"))
        if ok_all and task_id and not any(t.task_id == task_id for t in interp.task_updates):
            interp.task_updates.append(TaskUpdateSpec(task_id=task_id, status="done", result_summary="; ".join(pieces)[:300]))
            for uid in md.get("task_resolves_unknowns") or []:
                if results or reports or md.get("reasoning_output"):
                    # An unknown counts as resolved only when a specialist reported with confidence >= 0.6 or a tool produced a primary result.
                    if any(float(r.get("confidence", 0)) >= 0.6 for r in reports) or any(r.get("ok") and r.get("tool") in ("run_tests", "calculate", "read_file", "read_document") for r in results):
                        interp.resolved_unknowns.append(uid)
        interp.summary = "; ".join(pieces) or "no observable result"
        return interp.model_dump(mode="json")

    # specialist ---------------------------------------------------------------------

    def _specialist(self, req: CognitionRequest) -> dict[str, Any]:
        md = req.metadata
        spec = md.get("spec") or {}
        ctx = md.get("context") or {}
        claims = ctx.get("claims") or []
        return SpecialistReport(
            role=str(spec.get("role", "analyst")),
            conclusion=f"Heuristic specialist cannot perform open-ended {spec.get('role', 'analysis')} without a reasoning model; {len(claims)} claims were available in context.",
            confidence=0.2,
            unresolved=[str(spec.get("objective", ""))[:200]],
            blocked=bool(md.get("requires_model", True)),
            blocked_reason="no reasoning model available to the heuristic executive",
        ).model_dump(mode="json")

    # challenge ------------------------------------------------------------------------

    def _challenge(self, req: CognitionRequest) -> dict[str, Any]:
        md = req.metadata
        a = str(md.get("executive_position", ""))
        b = str(md.get("specialist_position", ""))
        same = _tokens(a) & _tokens(b)
        material = bool(a and b) and (len(same) / max(1, len(_tokens(a) | _tokens(b)))) < 0.5
        return {
            "disagreements": [
                {"topic": str(md.get("question", ""))[:200], "executive_position": a[:300], "specialist_position": b[:300], "material": material, "resolution_plan": "Gather a primary source that discriminates between the two positions"}
            ] if (a and b) else [],
            "summary": "positions compared by lexical overlap" if a and b else "insufficient positions",
        }

    # verify -----------------------------------------------------------------------------

    def _verify(self, req: CognitionRequest) -> dict[str, Any]:
        checks = list(req.metadata.get("checks") or [])
        failed = [c for c in checks if c.get("status") == "failed"]
        return VerificationJudgment(status="failed" if failed else ("passed" if checks else "inconclusive"), summary=f"{len(checks)} checks, {len(failed)} failed", issues=[c.get("detail", "") for c in failed], checked=[c.get("name", "") for c in checks], confidence=0.8 if checks else 0.3).model_dump(mode="json")

    # synthesize -------------------------------------------------------------------------

    def _synthesize(self, req: CognitionRequest) -> dict[str, Any]:
        md = req.metadata
        criteria = list(md.get("criteria") or [])
        claims = sorted(md.get("claims") or [], key=lambda c: -float(c.get("confidence", 0)) * float(c.get("decision_relevance", 0.5)))
        tournament = md.get("tournament") or {}
        contradictions = list(md.get("contradictions") or [])
        blocked = list(md.get("blocked") or [])
        unknowns = list(md.get("unknowns") or [])
        leading = tournament.get("leading_statement")
        top = [c.get("proposition") for c in claims[:3]]
        conclusion = leading or (top[0] if top else "Insufficient verified evidence to reach a conclusion")
        all_sat = bool(criteria) and all(c.get("satisfied") for c in criteria)
        status = "complete" if all_sat else ("blocked_external" if blocked and not all_sat else "active")
        return Synthesis(
            conclusion=str(conclusion),
            decision=str(leading or ""),
            rationale="Grounded in the highest-confidence verified claims: " + "; ".join(str(t) for t in top) if top else "No verified claims available.",
            criteria_assessment=[{"criterion_id": c["id"], "satisfied": bool(c.get("satisfied")), "evidence": c.get("evidence", "")} for c in criteria],
            remaining_uncertainties=[u.get("question", "") for u in unknowns][:5] + [c.get("description", "") for c in contradictions][:3],
            what_would_change_the_conclusion=[f"Evidence refuting: {t}" for t in top[:2]],
            confidence=min(0.9, 0.3 + 0.6 * (sum(1 for c in criteria if c.get("satisfied")) / max(1, len(criteria)))),
            mission_status=status,
            blocked_by="; ".join(b.get("what_would_unblock", "") for b in blocked)[:300],
        ).model_dump(mode="json")


def _tokens(s: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", s.lower()) if len(w) > 2}


class ScriptedExecutive:
    """Executive with injectable responses/policies per cognition kind, falling back to heuristics."""

    name = "scripted"

    def __init__(self, responses: Optional[dict[str, list[dict[str, Any]]]] = None, policies: Optional[dict[str, Policy]] = None, model: str = "scripted", fail_kinds: Optional[dict[str, str]] = None):
        self.responses = {k: list(v) for k, v in (responses or {}).items()}
        self.policies = dict(policies or {})
        self.fail_kinds = dict(fail_kinds or {})
        self.fallback = HeuristicExecutive(model=model)
        self.model = model
        self.calls: list[CognitionRequest] = []

    def call(self, req: CognitionRequest) -> CognitionResponse:
        self.calls.append(req)
        if req.kind in self.fail_kinds:
            return CognitionResponse(ok=False, model_requested=req.model, error=f"scripted failure for {req.kind}", error_kind=self.fail_kinds[req.kind])
        queue = self.responses.get(req.kind)
        if queue:
            parsed = queue.pop(0)
            if callable(parsed):
                parsed = parsed(req)
            return CognitionResponse(ok=True, parsed=parsed, model_requested=req.model, models_used=[self.model], turns=1)
        pol = self.policies.get(req.kind)
        if pol is not None:
            parsed = pol(req)
            if parsed is not None:
                return CognitionResponse(ok=True, parsed=parsed, model_requested=req.model, models_used=[self.model], turns=1)
        return self.fallback.call(req)
