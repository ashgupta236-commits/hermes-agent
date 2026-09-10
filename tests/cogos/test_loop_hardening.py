"""Regression tests for bugs found by an adversarial review of the executive loop.

Each test reproduces the original failure mode. They are the reason the loop cannot
livelock on a denied authorization, chain replans forever, re-run an unchanged criteria
pass, accumulate duplicate contradictions, or mark a mission complete without a passing
verification record.
"""

from __future__ import annotations

import json
from typing import Any

from cogos.adapters.scripted import HeuristicExecutive, ScriptedExecutive
from cogos.evaluation.support import Sandbox, engineer_policy
from cogos.governance.firewall import CapabilityFirewall
from cogos.beliefs import BeliefGraph
from cogos.executive.loop import OperationOutcome
from cogos.planner import Planner
from cogos.schemas.cognition import TaskSpec
from cogos.schemas.common import OperationKind, VerificationStatus
from cogos.schemas.mission import MissionStatus, TaskStatus
from cogos.verification import VerificationEngine, mission_completion_check
from cogos.world_model import WorldModelManager


# --- 1. denied authorization must not reactivate the task forever ---------------------------


def _blocked_shell_mission(sb: Sandbox) -> Any:
    """A mission whose only work is one destructive shell command the firewall must gate."""
    state = sb.runtime.new_mission("Build the feature described in REQUIREMENTS.md.", context=sb.context())
    # Drop the compiled plan so the destructive task is the only thing to do.
    state.tasks = []
    Planner(state).add_tasks_from_specs([
        TaskSpec(key="wipe", title="Clean the scratch directory", operation_hint="execute_action",
                 parameters_json=json.dumps({"tool": "shell", "arguments": {"command": "rm -rf /tmp/cogos-scratch-never"}}), priority=0.99)
    ])
    sb.runtime.store.save_mission(state, "tasks_added")
    return state


def _run_until_authorization_request(sb: Sandbox, state: Any, max_cycles: int = 12) -> Any:
    state = sb.runtime.run(state.mission_id, max_cycles=max_cycles)
    assert any(h.kind == "authorization" for h in state.human_requests), "the firewall must request authorization"
    return state


def test_denied_authorization_does_not_livelock_the_task() -> None:
    sb = Sandbox("denied", with_demo_project=True)
    sb.adapter.policies["specialist"] = engineer_policy(sb.root)
    try:
        state = _blocked_shell_mission(sb)
        state = _run_until_authorization_request(sb, state)
        request = next(h for h in state.human_requests if h.kind == "authorization")

        sb.runtime.answer(state.mission_id, request.id, "deny")
        state = sb.runtime.run(state.mission_id, max_cycles=30)

        wipe = next(t for t in state.tasks if t.title == "Clean the scratch directory")
        assert wipe.status is TaskStatus.CANCELLED, "a denied operation must be closed, not retried"
        assert wipe.attempts <= wipe.max_attempts
        assert len(state.blocked_operations) <= 3, "a denial must not append a blocked op every cycle"
        assert state.usage.cycles < 36, "the mission must not burn its budget on a denied operation"
        assert not any(v.action_class.value == "destructive" and v.decision.value == "allow" for v in sb.runtime.firewall.audit)
    finally:
        sb.cleanup()


def test_granted_authorization_still_bounds_attempts() -> None:
    sb = Sandbox("granted", with_demo_project=True)
    sb.adapter.policies["specialist"] = engineer_policy(sb.root)
    try:
        state = _blocked_shell_mission(sb)
        state = _run_until_authorization_request(sb, state)
        wipe = next(t for t in state.tasks if t.title == "Clean the scratch directory")
        wipe.attempts = wipe.max_attempts
        sb.runtime.store.save_mission(state, "attempts_exhausted")

        sb.runtime.authorize(state.mission_id, "destructive")
        state = sb.runtime.run(state.mission_id, max_cycles=20)
        wipe = next(t for t in state.tasks if t.title == "Clean the scratch directory")
        assert wipe.status in (TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.DONE)
        assert wipe.attempts <= wipe.max_attempts
    finally:
        sb.cleanup()


# --- 2/7. completion integrity rests on resolved, passing verification records ---------------


def test_model_cannot_satisfy_a_criterion_without_a_passing_record() -> None:
    sb = Sandbox("assert", with_demo_project=True)

    def interpret_policy(req: Any) -> Any:
        parsed = HeuristicExecutive()._interpret(req)
        parsed["criteria_satisfied"] = [c["id"] for c in req.metadata.get("criteria", [])] or [c for c in req.metadata.get("criterion_ids", [])]
        return parsed

    sb.adapter.policies["specialist"] = engineer_policy(sb.root)
    sb.adapter.policies["interpret"] = interpret_policy
    try:
        state = sb.runtime.new_mission("Build the feature described in REQUIREMENTS.md.", context=sb.context())
        criterion_ids = [c.id for c in state.success_criteria]
        state = sb.runtime.run(state.mission_id, max_cycles=30)
        for cid in criterion_ids:
            crit = next(c for c in state.success_criteria if c.id == cid)
            if crit.satisfied:
                assert state.passing_verifications(crit.verification_ids), f"{cid} satisfied with no passing record"
    finally:
        sb.cleanup()


def test_failed_verification_record_is_persisted_but_never_cited() -> None:
    sb = Sandbox("records", with_demo_project=True)
    try:
        state = sb.runtime.new_mission("Build the feature described in REQUIREMENTS.md.", context=sb.context())
        criterion = state.success_criteria[0]
        engine = VerificationEngine(sb.runtime.fabric, state)
        result = engine.verify_criterion(criterion)

        assert result.status is not VerificationStatus.PASSED
        assert state.verification(result.id) is not None, "the attempt must be auditable"
        assert result.id not in criterion.verification_ids, "a failed record is not evidence"

        criterion.satisfied = True
        criterion.verification_ids.append(result.id)  # simulate a forged citation
        gate = mission_completion_check(state)
        assert gate.status is not VerificationStatus.PASSED
        assert "no passing verification record" in gate.summary
    finally:
        sb.cleanup()


# --- 3. a reproduction run is never evidence that the suite passes ---------------------------


def test_reproduction_run_does_not_launder_a_failing_suite() -> None:
    sb = Sandbox("repro", with_demo_project=True)
    (sb.root / "test_broken.py").write_text("def test_fails():\n    assert False\n", encoding="utf-8")
    try:
        state = sb.runtime.new_mission("Build the feature described in REQUIREMENTS.md.", context=sb.context())
        task = state.tasks[0]
        task.parameters.update({"commands": [sb.context()["test_command"]], "expect_failure": True})
        engine = VerificationEngine(sb.runtime.fabric, state)
        before = len(state.tests)
        engine.verify_code(list(task.parameters["commands"]), task_id=task.id)
        for rec in state.tests[before:]:
            rec.expected_failure = True

        assert all(r.status is VerificationStatus.FAILED for r in state.tests[before:]), "the ledger keeps the observed status"
        assert all(r.expected_failure for r in state.tests[before:])

        criterion = state.success_criteria[-1]
        criterion.verification_method = "run pytest"
        res = VerificationEngine(sb.runtime.fabric, state).verify_criterion(criterion)
        assert res.status is VerificationStatus.FAILED, "a reproduction must not satisfy a 'tests pass' criterion"
        assert not criterion.satisfied
    finally:
        sb.cleanup()


# --- 4. replanning is bounded ----------------------------------------------------------------


def test_replan_chains_are_bounded() -> None:
    counter = {"n": 0}

    def replan_policy(_req: Any) -> Any:
        counter["n"] += 1
        return {"rationale": "try another way", "give_up": False, "blocked_by": "", "what_would_unblock": "",
                "new_tasks": [{"key": f"alt{counter['n']}", "title": f"Alternative approach {counter['n']}", "description": "",
                               "goal_key": "", "depends_on": [], "operation_hint": "use_external_tool",
                               "parameters_json": json.dumps({"tool": "always_broken", "arguments": {}}),
                               "priority": 0.9, "parallel_safe": False, "resolves_unknowns": [], "addresses_criteria": []}]}

    sb = Sandbox("replan", with_demo_project=True, adapter=ScriptedExecutive(policies={"replan": replan_policy}))
    sb.adapter.policies["specialist"] = engineer_policy(sb.root)

    from cogos.schemas.tools import ToolSpec

    def broken(_a: dict[str, Any], _c: Any) -> dict[str, Any]:
        raise ValueError("always broken")

    sb.runtime.fabric.register(ToolSpec(name="always_broken", description="always fails", substrate="api"), broken)
    try:
        state = sb.runtime.new_mission("Build the feature described in REQUIREMENTS.md.", context=sb.context())
        Planner(state).add_tasks_from_specs([
            TaskSpec(key="seed", title="Seed failing task", operation_hint="use_external_tool",
                     parameters_json=json.dumps({"tool": "always_broken", "arguments": {}}), priority=0.99, parallel_safe=False)
        ])
        sb.runtime.store.save_mission(state, "seeded")
        state = sb.runtime.run(state.mission_id, max_cycles=60)

        alts = [t for t in state.tasks if t.title.startswith("Alternative approach")]
        assert len(alts) <= 6, f"replan chain must be bounded, got {len(alts)} replacement tasks"
        assert state.status is not MissionStatus.ACTIVE, "the loop must reach a terminal state"
    finally:
        sb.cleanup()


# --- 5. a verify step with no target is bound to real pending work ---------------------------


def test_verify_without_target_does_not_spin() -> None:
    def select_policy(req: Any) -> Any:
        return {"operation": "verify", "task_id": "", "rationale": "check everything", "expected_outcome": "",
                "confidence": 0.8, "alternatives_considered": [], "tool_calls": [], "specialists": [],
                "reasoning_output": "", "human_request": None, "calculation": "", "simulation_json": "{}",
                "wait_for_event_kind": "", "consequential": False}

    sb = Sandbox("verifyspin", with_demo_project=True, adapter=ScriptedExecutive(policies={"select": select_policy}))
    sb.adapter.policies["specialist"] = engineer_policy(sb.root)
    try:
        state = sb.runtime.new_mission("Build the feature described in REQUIREMENTS.md.", context=sb.context())
        state = sb.runtime.run(state.mission_id, max_cycles=25)
        dead = [v for v in state.verifications if v.target_type == "criteria" and "skipped" in v.summary]
        criteria_records = [v for v in state.verifications if v.target_type == "criteria"]
        assert len(criteria_records) - len(dead) <= 4, "the criteria pass must not re-run on unchanged state"
        for c in state.success_criteria:
            assert len(c.verification_ids) <= 3, "dead verification ids must not accumulate on a criterion"
    finally:
        sb.cleanup()


# --- 6. contradictions with unresolvable claim ids are deduped -------------------------------


def test_unresolvable_contradiction_is_deduped() -> None:
    def interpret_policy(req: Any) -> Any:
        parsed = HeuristicExecutive()._interpret(req)
        parsed["contradictions"] = [{"claim_ids": ["clm_does_not_exist"], "description": "Sources disagree on the market size",
                                     "severity": 0.8, "suspected_cause": "scope"}]
        return parsed

    sb = Sandbox("contradict", with_demo_project=True, adapter=ScriptedExecutive(policies={"interpret": interpret_policy}))
    sb.adapter.policies["specialist"] = engineer_policy(sb.root)
    try:
        state = sb.runtime.new_mission("Build the feature described in REQUIREMENTS.md.", context=sb.context())
        state = sb.runtime.run(state.mission_id, max_cycles=25)
        assert len(state.contradictions) == 1, f"one repeated contradiction must dedupe, got {len(state.contradictions)}"
        assert "unresolved claim ids" in state.contradictions[0].description
    finally:
        sb.cleanup()


# --- 8. an invalidating event demotes a standing claim ---------------------------------------


def test_invalidating_event_demotes_a_supported_claim() -> None:
    from cogos.beliefs import BeliefGraph
    from cogos.schemas.beliefs import Claim, ClaimStatus, Evidence, EvidenceKind
    from cogos.schemas.common import Provenance

    sb = Sandbox("invalidate", with_demo_project=True)
    try:
        state = sb.runtime.new_mission("Build the feature described in REQUIREMENTS.md.", context=sb.context())
        graph = BeliefGraph(state)
        claim = graph.add_claim(Claim(proposition="The rounding mode is half-even", confidence=0.9, decision_relevance=0.9))
        graph.add_evidence(Evidence(summary="Spec section 3", supports_claim_ids=[claim.id], kind=EvidenceKind.PRIMARY,
                                    provenance=Provenance(source="file:SPEC.md", reliability=0.9)))
        sb.runtime.store.save_mission(state, "claim_seeded")
        assert state.claim(claim.id).status in (ClaimStatus.SUPPORTED, ClaimStatus.ESTABLISHED)

        sb.runtime.events.subscribe(state.mission_id, "human_input", affects={"claims": [claim.id]})
        sb.runtime.inform(state.mission_id, "The spec was revised: rounding is now half-up.")
        state = sb.runtime.run(state.mission_id, max_cycles=2)

        assert state.claim(claim.id).status is ClaimStatus.STALE, "new information must demote a standing claim"
        assert any("demoted to stale" in n for n in state.notes)
    finally:
        sb.cleanup()


# --- 9. web-search delegation respects the subagent budget -----------------------------------


def test_web_search_delegation_respects_the_subagent_budget() -> None:
    sb = Sandbox("websearch", with_demo_project=True)
    sb.adapter.policies["specialist"] = engineer_policy(sb.root)
    try:
        state = sb.runtime.new_mission("Build the feature described in REQUIREMENTS.md.", context=sb.context())
        state.budget.max_subagents = 0
        Planner(state).add_tasks_from_specs([
            TaskSpec(key="search", title="Look up rounding conventions", operation_hint="search",
                     parameters_json=json.dumps({"tool": "web_search", "arguments": {"query": "python round half even"}}), priority=0.99)
        ])
        sb.runtime.store.save_mission(state, "search_added")
        state = sb.runtime.run(state.mission_id, max_cycles=8)
        assert state.usage.subagents_spawned == 0, "a zero subagent budget must block delegation"
    finally:
        sb.cleanup()


def test_firewall_has_no_unreachable_duplicate_branch() -> None:
    """The consequential/security/privacy classes are decided by the single always_require_human rule."""
    from cogos.config import GovernanceConfig
    from cogos.schemas.common import PolicyDecision
    from cogos.schemas.tools import ToolCall, ToolSpec
    import pathlib

    cfg = GovernanceConfig(always_require_human=["consequential_shared"])
    fw = CapabilityFirewall(cfg, pathlib.Path.cwd())
    spec = ToolSpec(name="shell", description="", substrate="shell")
    verdict = fw.check(ToolCall(tool="shell", arguments={"command": "git push origin main"}), spec)
    assert verdict.decision is PolicyDecision.REQUIRE_HUMAN
    fw.grant("consequential_shared")
    assert fw.check(ToolCall(tool="shell", arguments={"command": "git push origin main"}), spec).decision is PolicyDecision.ALLOW


def test_a_contradiction_settled_by_scope_is_resolved_not_merely_re_reported():
    """Found in a live run: the executive correctly reasoned that "the files do not exist" and
    "the files exist" were each true of their own period — but the only channel available
    appended a new severity-0 record while the original severity-1.0 contradiction stayed open,
    so the controller re-issued must_falsify every cycle against a settled dispute."""
    from cogos.schemas.beliefs import Contradiction
    from cogos.schemas.cognition import ContradictionSpec, ObservationInterpretation

    sb = Sandbox("contradiction-resolution")
    try:
        state = sb.runtime.new_mission("Build the thing", context=sb.context(has_requirements=False))
        stale = Contradiction(claim_ids=["clm_a"], description="files do not exist vs files exist", severity=1.0)
        state.contradictions.append(stale)
        assert [c.id for c in state.unresolved_contradictions()] == [stale.id]

        interp = ObservationInterpretation(
            summary="settled by period",
            contradictions=[
                ContradictionSpec(
                    claim_ids=[],
                    description="",
                    severity=0.0,
                    suspected_cause="time_period",
                    resolves_contradiction_ids=[stale.id],
                    resolution="the baseline claim describes the pre-write state; the existence claim the post-write state; both true within their periods",
                )
            ],
        )
        sb.runtime.executive._apply_interpretation(
            state, interp, OperationOutcome(operation=OperationKind.DIRECT_REASONING), None,
            BeliefGraph(state), WorldModelManager(state.world_model), Planner(state),
        )

        assert stale.resolved is True
        assert "both true within their periods" in stale.resolution
        assert state.unresolved_contradictions() == []
        assert len(state.contradictions) == 1, "a resolution resolves; it does not append another record"
    finally:
        sb.cleanup()


def test_a_resolution_without_a_stated_reason_is_refused():
    """A bare assertion that a contradiction is resolved is not a resolution."""
    from cogos.schemas.beliefs import Contradiction
    from cogos.schemas.cognition import ContradictionSpec, ObservationInterpretation

    sb = Sandbox("contradiction-bare")
    try:
        state = sb.runtime.new_mission("Build the thing", context=sb.context(has_requirements=False))
        stale = Contradiction(claim_ids=["clm_a"], description="a vs b", severity=1.0)
        state.contradictions.append(stale)

        interp = ObservationInterpretation(
            summary="no reason given",
            contradictions=[ContradictionSpec(claim_ids=[], description="", severity=0.0, resolves_contradiction_ids=[stale.id], resolution="   ")],
        )
        sb.runtime.executive._apply_interpretation(
            state, interp, OperationOutcome(operation=OperationKind.DIRECT_REASONING), None,
            BeliefGraph(state), WorldModelManager(state.world_model), Planner(state),
        )
        assert stale.resolved is False
        assert state.unresolved_contradictions() == [stale]
    finally:
        sb.cleanup()
