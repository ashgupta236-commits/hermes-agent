"""Budget admission, progress gradient and run provenance (live-run areas F, G, I).

Live evidence: a 2400s wall-clock cap became 2940s and a $19.00 cost cap became $20.51, because
the budget was checked at cycle start and then a 486-second, $2.53 call was allowed to begin.
Spend climbed across eleven cycles while satisfied criteria and bound receipts stayed at zero the
whole time. And the mission kept running while fixes were committed, so nothing in the trace could
say which build produced it.
"""

from __future__ import annotations

import pytest

from cogos.adapters.base import BudgetExhausted, CognitionRequest
from cogos.evaluation.support import Sandbox
from cogos.observability.ledger import ResourceLedger
from cogos.provenance import capture
from cogos.executive.progress import FLAT_PROGRESS_CYCLES, measure, track
from cogos.schemas.mission import Artifact, Budget, MissionState, ResourceUsage, SuccessCriterion, Task, TaskStatus


# == F: admission control ======================================================================


def _ledger(cost: float = 0.0, seconds: float = 0.0, calls: int = 0) -> ResourceLedger:
    return ResourceLedger(ResourceUsage(estimated_cost_usd=cost, wall_clock_seconds=seconds, model_calls=calls))


def test_a_call_that_would_breach_the_cost_cap_is_refused_before_it_starts():
    """The live overshoot in one assertion: $18.00 spent, a $2.53 call, a $19.00 cap."""
    ledger = _ledger(cost=18.0)
    budget = Budget(max_cost_usd=19.0)
    assert ledger.admit(budget, estimated_cost_usd=0.5) is None
    refusal = ledger.admit(budget, estimated_cost_usd=2.53)
    assert refusal and "would exceed the cost budget" in refusal
    assert "$18.00 spent" in refusal and "$19.00" in refusal


def test_a_call_that_would_breach_the_wall_clock_cap_is_refused_before_it_starts():
    ledger = _ledger(seconds=2300)
    budget = Budget(max_wall_clock_seconds=2400)
    assert ledger.admit(budget, estimated_seconds=50) is None
    refusal = ledger.admit(budget, estimated_seconds=486)
    assert refusal and "wall-clock budget" in refusal


def test_reserved_spend_counts_against_admission():
    ledger = _ledger(cost=17.0)
    budget = Budget(max_cost_usd=19.0)
    assert ledger.admit(budget, estimated_cost_usd=1.5) is None
    ledger.reserve(calls=1, cost_usd=1.0)
    refusal = ledger.admit(budget, estimated_cost_usd=1.5)
    assert refusal and "reserved" in refusal


def test_the_affordable_ceiling_shrinks_as_the_mission_spends():
    budget = Budget(max_cost_usd=10.0)
    assert _ledger(cost=0.0).affordable_cost(budget) == 10.0
    assert _ledger(cost=7.5).affordable_cost(budget) == 2.5
    assert _ledger(cost=12.0).affordable_cost(budget) == 0.0
    assert _ledger().affordable_cost(Budget()) is None, "an unbounded budget has no per-call ceiling"


def test_the_per_call_ceiling_reaches_the_provider():
    """Argument analysis bounds what we ask for; this bounds what the call can actually spend."""
    from cogos.adapters.claude_code import ClaudeCodeExecutive

    adapter = ClaudeCodeExecutive("claude-fable-5-1")
    req = CognitionRequest(kind="select", system_prompt="s", prompt="p", schema_name="X", output_schema={"type": "object"}, model="claude-fable-5-1", max_cost_usd=1.25)
    cmd = adapter.build_command(req)
    assert "--max-budget-usd" in cmd
    assert cmd[cmd.index("--max-budget-usd") + 1] == "1.2500"

    unbounded = adapter.build_command(req.model_copy(update={"max_cost_usd": None}))
    assert "--max-budget-usd" not in unbounded


def test_budget_refusal_pauses_the_mission_and_never_completes_it():
    """Exhaustion must be a stop, never a route to success."""
    sb = Sandbox("admission-pause", with_demo_project=True)
    try:
        state = sb.runtime.new_mission("Build the feature described in REQUIREMENTS.md.", context=sb.context())
        state.budget.max_cost_usd = 0.0000001
        state.usage.estimated_cost_usd = 0.001
        sb.runtime.store.save_mission(state, "starved")
        state = sb.runtime.run(state.mission_id, max_cycles=5)

        assert state.status.value != "complete", "an unaffordable mission must never complete"
        assert state.status.value == "paused"
        assert any("budget" in n for n in state.notes)
        assert state.executive_model == sb.config.executive.model, "budget pressure never changes the model"
    finally:
        sb.cleanup()


def test_a_resumed_mission_never_resets_its_cumulative_spend():
    sb = Sandbox("admission-resume", with_demo_project=True)
    try:
        state = sb.runtime.new_mission("Build the feature described in REQUIREMENTS.md.", context=sb.context())
        state.usage.estimated_cost_usd = 8.81
        state.usage.model_calls = 14
        sb.runtime.store.save_mission(state, "mid_flight")

        sb.reopen()
        resumed = sb.runtime.store.load_mission(state.mission_id)
        assert resumed is not None
        assert resumed.usage.estimated_cost_usd == pytest.approx(8.81)
        assert resumed.usage.model_calls == 14

        ledger = ResourceLedger(resumed.usage)
        refusal = ledger.admit(Budget(max_cost_usd=9.0), estimated_cost_usd=1.0)
        assert refusal, "the resumed segment is charged against cumulative, not incremental, spend"
    finally:
        sb.cleanup()


def test_the_cost_model_is_learned_from_the_missions_own_calls():
    """Estimates come from measurement, not a guess, and survive with the mission."""
    sb = Sandbox("admission-costmodel")
    try:
        state = sb.runtime.new_mission("Build it", context=sb.context(has_requirements=False))
        ex = sb.runtime.executive

        class _Resp:
            cost_usd = 2.53
            duration_ms = 486_000

        before = ex._estimate_call(state, "interpret")
        assert before["cost_usd"] > 0 and before["seconds"] > 0, "a floor applies before any history"

        ex._record_call_cost(state, "interpret", _Resp())
        after = ex._estimate_call(state, "interpret")
        assert after["cost_usd"] == pytest.approx(2.53)
        assert after["seconds"] == pytest.approx(486.0)
        assert state.resources["cost_model"]["interpret"]["calls"] == 1
    finally:
        sb.cleanup()


# == G: progress measured by what can be checked ===============================================


def test_progress_counts_verifiable_outcomes_not_activity():
    state = MissionState(objective="Build it")
    assert measure(state).total() == 0

    state.tasks.append(Task(title="Wrote a file", status=TaskStatus.DONE))
    state.artifacts.append(Artifact(name="a.txt", path="/nonexistent/a.txt", verified=True))
    progress = measure(state)
    assert progress.completed_tasks == 1
    assert progress.verified_artifacts == 0, "an artifact that is not intact is not progress"
    assert progress.criteria_with_receipts == 0


def test_a_criterion_counts_only_once_a_receipt_is_bound_to_it():
    from cogos.schemas.common import VerificationStatus
    from cogos.schemas.verification import VerificationResult, cite

    state = MissionState(objective="Build it")
    criterion = SuccessCriterion(description="It works")
    state.success_criteria.append(criterion)
    criterion.satisfied = True
    assert measure(state).criteria_with_receipts == 0, "satisfied without a receipt is not progress"

    receipt = VerificationResult(target_type="criterion", target_id=criterion.id, status=VerificationStatus.PASSED, summary="checked")
    state.verifications.append(receipt)
    cite(criterion.verification_ids, receipt, criterion.id, target_type="criterion")
    assert measure(state).criteria_with_receipts == 1


def test_rising_spend_against_flat_progress_is_surfaced_as_an_observation():
    """The live curve: spend climbing while nothing checkable moved."""
    state = MissionState(objective="Build it")
    observation = None
    for _ in range(FLAT_PROGRESS_CYCLES + 1):
        state.usage.estimated_cost_usd += 2.0
        state.usage.model_calls += 2
        observation = track(state)

    assert observation is not None and observation.flat is True
    assert observation.cycles_flat >= FLAT_PROGRESS_CYCLES
    assert observation.spend_since_flat_usd >= 2.0
    assert "Activity is not progress" in observation.note
    # It is an observation, not an instruction — it names no action to take.
    assert "must" not in observation.note.lower()


def test_real_progress_resets_the_window():
    state = MissionState(objective="Build it")
    for _ in range(FLAT_PROGRESS_CYCLES + 1):
        state.usage.estimated_cost_usd += 2.0
        track(state)
    assert track(state).flat is True

    state.tasks.append(Task(title="Something checkable", status=TaskStatus.DONE))
    fresh = track(state)
    assert fresh.flat is False and fresh.cycles_flat == 0


def test_the_progress_record_survives_checkpoint_and_resume():
    sb = Sandbox("progress-resume", with_demo_project=True)
    try:
        state = sb.runtime.new_mission("Build the feature described in REQUIREMENTS.md.", context=sb.context())
        for _ in range(FLAT_PROGRESS_CYCLES + 1):
            state.usage.estimated_cost_usd += 1.0
            track(state)
        flat_before = state.resources["progress"]["cycles_flat"]
        sb.runtime.store.save_mission(state, "flat")

        sb.reopen()
        resumed = sb.runtime.store.load_mission(state.mission_id)
        assert resumed is not None
        assert resumed.resources["progress"]["cycles_flat"] == flat_before
    finally:
        sb.cleanup()


# == I: run provenance =========================================================================


def test_provenance_identifies_the_implementation_not_the_workspace():
    """The live mission ran while fixes were committed; a trace must say which build made it."""
    sb = Sandbox("provenance")
    try:
        state = sb.runtime.new_mission("Build it", context=sb.context(has_requirements=False))
        prov = state.resources["provenance"]

        assert prov["git_commit"], "the implementation's commit is recorded"
        assert prov["git_branch"]
        assert prov["git_dirty"] in (True, False), "dirty state is established, not guessed"
        assert prov["adapter"] == "scripted"
        assert prov["model_requested"] == sb.config.executive.model
        assert prov["config_hash"] and prov["mission_schema_version"] == state.schema_version
        assert prov["python_version"] and prov["platform"]
        # The workspace is recorded separately because a mission can operate on any directory.
        assert prov["workspace"] == str(sb.root)
    finally:
        sb.cleanup()


def test_each_run_segment_records_its_own_build_and_starting_spend():
    """A resumed mission may be served by a different build than the one that started it."""
    sb = Sandbox("provenance-segments", with_demo_project=True)
    try:
        state = sb.runtime.new_mission("Build the feature described in REQUIREMENTS.md.", context=sb.context())
        sb.runtime.run(state.mission_id, max_cycles=2)
        sb.reopen()
        sb.runtime.run(state.mission_id, max_cycles=2)

        resumed = sb.runtime.store.load_mission(state.mission_id)
        assert resumed is not None
        segments = resumed.resources["run_segments"]
        assert len(segments) >= 2, "each run segment stamps its own provenance"
        assert all(s["git_commit"] for s in segments)
        assert segments[1]["started_at_cycle"] >= segments[0]["started_at_cycle"]
        assert "spend_at_start_usd" in segments[1]
    finally:
        sb.cleanup()


def test_provenance_capture_never_raises():
    class _Broken:
        name = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))

    prov = capture(object(), _Broken(), 1)
    assert prov.captured_at, "capture degrades to unknowns rather than failing a mission"
