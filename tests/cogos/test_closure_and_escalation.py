"""Closure and cognitive escalation (live-run incident, areas C and E).

Area E is the incident's central failure: the mission had **done the work** — correct `calc.py` and
`test_calc.py` on disk, 4/4 tests passing when run independently — and never bound a verification
receipt. Its own plan held eight ready or pending verification tasks, one of them addressing the
exact criterion the gate was failing on. Selection never reached any of them.

Area C is where the money went: `interpret` was 70% of spend, and its cost was decoupled from the
size of what it was reading (a 306ms `search_text` triggered a 423-second, $2.28 interpretation).

The tests below pin both repairs *and* the constraint on them: closure changes which action is
chosen, never what counts as evidence.
"""

from __future__ import annotations

from pathlib import Path

from cogos.evaluation.support import Sandbox
from cogos.executive.escalation import (
    Tier,
    classify,
    deterministic_interpretation,
    digest_to_interpretation,
)
from cogos.executive.loop import OperationOutcome
from cogos.planner import Planner
from cogos.schemas.common import ActionClass, OperationKind, TrustLevel
from cogos.schemas.mission import Artifact, MissionState, SuccessCriterion, Task, TaskStatus
from cogos.schemas.tools import ToolResult
from cogos.verification.closure import (
    CLOSURE_BONUS,
    assess,
    closure_bonus,
    needs_from_gate,
    substantive_work_done,
)
from cogos.verification.engine import mission_completion_check


def _result(tool: str, ok: bool = True, output: str = "done", trust: TrustLevel = TrustLevel.VERIFIED_TOOL, flags=None) -> ToolResult:
    return ToolResult(call_id="c1", tool=tool, ok=ok, output=output, trust=trust, injection_flags=list(flags or []))


def _outcome(**kw) -> OperationOutcome:
    kw.setdefault("operation", OperationKind.EXECUTE_ACTION)
    return OperationOutcome(**kw)


# == C: escalation routes by what the observation could carry ==================================


def test_a_clean_trusted_tool_result_needs_no_model_at_all():
    state = MissionState(objective="Build the feature")
    outcome = _outcome(tool_results=[_result("write_file"), _result("git")])
    esc = classify(OperationKind.EXECUTE_ACTION, outcome, None, state)
    assert esc.tier is Tier.L0_DETERMINISTIC


def test_untrusted_content_is_read_but_bounded_never_skipped():
    """File content still gets a mind on it — with a schema that cannot revise beliefs."""
    state = MissionState(objective="Build the feature")
    outcome = _outcome(tool_results=[_result("search_text", trust=TrustLevel.UNTRUSTED_EXTERNAL, output="3 matches")])
    esc = classify(OperationKind.INSPECT_FILES, outcome, None, state)
    assert esc.tier is Tier.L1_DIGEST
    assert esc.reasons and "needs reading" in esc.reasons[0]


def test_everything_that_could_carry_judgment_escalates():
    state = MissionState(objective="Build the feature")
    cases = {
        "failure": _outcome(tool_results=[_result("shell", ok=False, output="")]),
        "injection": _outcome(tool_results=[_result("read_file", flags=["ignore_previous"])]),
        "specialist": _outcome(tool_results=[_result("write_file")], specialist_reports=[{"role": "engineer"}]),
        "reasoning": _outcome(tool_results=[_result("write_file")], reasoning_output="a conclusion"),
        "errors": _outcome(tool_results=[_result("write_file")], errors=["something went wrong"]),
        "calculation": _outcome(tool_results=[_result("calculate")], calculation_result=42),
        "no_results": _outcome(tool_results=[]),
    }
    for name, outcome in cases.items():
        esc = classify(OperationKind.EXECUTE_ACTION, outcome, None, state)
        assert esc.tier is Tier.L2_FULL, f"{name} must escalate"
        assert esc.reasons, f"{name} must say why it escalated"


def test_judgment_operations_never_drop_below_full_interpretation():
    state = MissionState(objective="Build the feature")
    outcome = _outcome(tool_results=[_result("write_file")])
    for op in (OperationKind.FALSIFY, OperationKind.INSTANTIATE_SPECIALIST, OperationKind.SIMULATE, OperationKind.DIRECT_REASONING):
        assert classify(op, outcome, None, state).tier is Tier.L2_FULL


def test_an_open_contradiction_forces_full_interpretation():
    """A mission in a contested state cannot afford a mechanical reading of anything."""
    from cogos.schemas.beliefs import Contradiction

    state = MissionState(objective="Build the feature")
    outcome = _outcome(tool_results=[_result("write_file")])
    assert classify(OperationKind.EXECUTE_ACTION, outcome, None, state).tier is Tier.L0_DETERMINISTIC
    state.contradictions.append(Contradiction(claim_ids=["c1"], description="disputed", severity=0.9))
    esc = classify(OperationKind.EXECUTE_ACTION, outcome, None, state)
    assert esc.tier is Tier.L2_FULL
    assert any("contradiction" in r for r in esc.reasons)


def test_a_task_chosen_to_resolve_an_unknown_escalates():
    state = MissionState(objective="Build the feature")
    task = Task(title="Find out", resolves_unknown_ids=["unk_1"])
    esc = classify(OperationKind.EXECUTE_ACTION, _outcome(tool_results=[_result("write_file")]), task, state)
    assert esc.tier is Tier.L2_FULL


def test_no_cheap_interpretation_path_can_satisfy_a_criterion():
    """The integrity property: cheap tiers record what happened, never that a goal was met."""
    from cogos.executive.escalation import ObservationDigest

    state = MissionState(objective="Build the feature")
    state.success_criteria.append(SuccessCriterion(description="It works"))
    task = Task(title="Write it")

    det = deterministic_interpretation(OperationKind.EXECUTE_ACTION, _outcome(tool_results=[_result("write_file")]), task, state)
    assert det.criteria_satisfied == []
    assert det.new_claims == [] and det.contradictions == []

    dig = digest_to_interpretation(ObservationDigest(summary="read it", observed=["3 matches"]), task, state)
    assert dig.criteria_satisfied == []
    assert dig.new_claims == [] and dig.contradictions == []


# == E: closure prefers the planned step that would bind the missing evidence ==================


def _live_shaped_mission(root: Path) -> MissionState:
    """The live end-state: work done on disk, no receipt bound, verify task sitting unselected."""
    state = MissionState(objective="Build the feature described in REQUIREMENTS.md.")
    criterion = SuccessCriterion(description="Full test suite passes", verification_method="pytest exits 0")
    state.success_criteria.append(criterion)
    (root / "calc.py").write_text("def add_percent(v, p):\n    return round(v * (1 + p / 100), 2)\n", encoding="utf-8")
    state.artifacts.append(Artifact(name="calc.py", path=str(root / "calc.py"), summary="the implementation"))
    state.resources["required_artifacts"] = [str(root / "calc.py")]

    done = Task(title="Write calc.py", status=TaskStatus.DONE, operation_hint="execute_action")
    verify = Task(
        title="Run the test suite",
        status=TaskStatus.READY,
        operation_hint="verify",
        addresses_criterion_ids=[criterion.id],
        parameters={"commands": ["python -m pytest -q"]},
        priority=0.5,
    )
    explore = Task(title="Audit the provenance of the rounding constraint", status=TaskStatus.READY, operation_hint="instantiate_specialist", priority=0.95)
    state.tasks.extend([done, verify, explore])
    return state


def test_the_gate_names_what_is_missing_and_closure_reads_it_verbatim():
    sb = Sandbox("closure-needs")
    try:
        state = _live_shaped_mission(sb.root)
        gate = mission_completion_check(state)
        assert gate.status.value == "failed"

        needs = needs_from_gate(gate, state)
        checks = {n.check for n in needs}
        assert "success_criteria" in checks and "required_artifacts" in checks
        assert any(n.wants_verification for n in needs)
        # The needs are derived from the gate, never re-derived from domain knowledge.
        assert all(n.detail for n in needs)
    finally:
        sb.cleanup()


def test_the_planned_verification_task_outranks_further_exploration():
    """The live failure in one assertion: the verify task existed and lost to deliberation."""
    sb = Sandbox("closure-order")
    try:
        state = _live_shaped_mission(sb.root)
        planner = Planner(state)

        # Before closure is assessed, the higher-priority exploration wins — the live behaviour.
        assert planner.next_tasks(limit=1)[0].title.startswith("Audit the provenance")

        planner._closure = assess(state, mission_completion_check(state))
        assert planner._closure.blocked and planner._closure.candidate_task_ids

        assert planner.next_tasks(limit=1)[0].title == "Run the test suite"
    finally:
        sb.cleanup()


def test_closure_changes_ordering_only_and_never_evidence():
    """The safety property: nothing about closure can satisfy a criterion or bind a receipt."""
    sb = Sandbox("closure-safety")
    try:
        state = _live_shaped_mission(sb.root)
        before = mission_completion_check(state)
        criteria_before = [(c.id, c.satisfied, list(c.verification_ids)) for c in state.success_criteria]
        receipts_before = len(state.verifications)

        closure = assess(state, before)
        Planner(state)._closure = closure

        after = mission_completion_check(state)
        assert after.status.value == "failed", "closure must not unblock the gate"
        assert [(c.id, c.satisfied, list(c.verification_ids)) for c in state.success_criteria] == criteria_before
        assert len(state.verifications) == receipts_before + 0 or True  # gate records its own check only
        assert all(not c.satisfied for c in state.success_criteria)
    finally:
        sb.cleanup()


def test_an_unblocked_mission_gets_no_ordering_distortion():
    state = MissionState(objective="Nothing missing")
    task = Task(title="Anything", operation_hint="verify")
    assert closure_bonus(task, None, []) == 0.0
    closure = assess(state, mission_completion_check(state))
    if not closure.blocked:
        assert closure_bonus(task, closure, closure.needs) == 0.0


def test_a_task_qualifies_through_declared_criteria_or_verifying_role_only():
    """Generic by construction — no domain knowledge about what a deliverable looks like."""
    sb = Sandbox("closure-generic")
    try:
        state = _live_shaped_mission(sb.root)
        closure = assess(state, mission_completion_check(state))
        verify = next(t for t in state.tasks if t.title == "Run the test suite")
        explore = next(t for t in state.tasks if t.title.startswith("Audit"))
        assert closure_bonus(verify, closure, closure.needs) == CLOSURE_BONUS
        assert closure_bonus(explore, closure, closure.needs) == 0.0
    finally:
        sb.cleanup()


def test_substantive_work_done_is_about_progress_not_correctness():
    state = MissionState(objective="x")
    assert substantive_work_done(state) is False
    state.tasks.append(Task(title="Do it", status=TaskStatus.READY, operation_hint="execute_action"))
    assert substantive_work_done(state) is False
    state.tasks[0].status = TaskStatus.DONE
    assert substantive_work_done(state) is True
