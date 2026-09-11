"""A criterion the deterministic checks cannot decide must resolve, not loop.

Real-model validation showed the runtime re-selecting `verify` for six consecutive
cycles because the criteria pass kept returning `inconclusive` and nothing changed.
These tests pin the convergence rules that fixed it.
"""

from __future__ import annotations

from typing import Any

from cogos.evaluation.support import Sandbox, engineer_policy
from cogos.schemas.mission import MissionStatus, SuccessCriterion


def _unparseable_criteria(sb: Sandbox, state: Any) -> Any:
    """Replace the compiled criteria with one no deterministic check can decide."""
    state.success_criteria = [SuccessCriterion(description="The feature is genuinely good", verification_method="reviewer judgement of overall quality")]
    for t in state.tasks:
        t.addresses_criterion_ids = [state.success_criteria[0].id]
    sb.runtime.store.save_mission(state, "criteria_replaced")
    return state


def _count_criteria_passes(sb: Sandbox, mission_id: str) -> int:
    return sum(1 for t in sb.runtime.store.traces(mission_id, limit=5000) if t.kind == "verify" and t.summary.startswith("criteria:"))


def test_undecidable_criterion_is_recorded_and_not_re_verified() -> None:
    sb = Sandbox("undecidable", with_demo_project=True)
    sb.adapter.policies["specialist"] = engineer_policy(sb.root)
    sb.adapter.policies["verify"] = lambda _req: {"status": "inconclusive", "summary": "cannot judge quality from the evidence", "issues": [], "confidence": 0.3, "checked": []}
    try:
        state = sb.runtime.new_mission("Build the feature described in REQUIREMENTS.md.", context=sb.context())
        _unparseable_criteria(sb, state)
        state = sb.runtime.run(state.mission_id, max_cycles=40)

        controller = state.resources.get("controller", {})
        assert controller.get("undecidable_criteria"), "an undecidable criterion must be recorded"
        assert _count_criteria_passes(sb, state.mission_id) <= 3, "the criteria pass must not repeat once nothing can change"
        assert state.status is not MissionStatus.COMPLETE, "an unverified criterion can never be marked complete"
        assert state.usage.cycles < 40, "the loop must reach a terminal state instead of burning the budget"
    finally:
        sb.cleanup()


def test_executive_judgement_can_satisfy_a_criterion_only_with_named_checks() -> None:
    sb = Sandbox("judged", with_demo_project=True)
    sb.adapter.policies["specialist"] = engineer_policy(sb.root)
    # passed but with no named checks -> must NOT satisfy the criterion
    sb.adapter.policies["verify"] = lambda _req: {"status": "passed", "summary": "looks right", "issues": [], "confidence": 0.95, "checked": []}
    try:
        state = sb.runtime.new_mission("Build the feature described in REQUIREMENTS.md.", context=sb.context())
        _unparseable_criteria(sb, state)
        state = sb.runtime.run(state.mission_id, max_cycles=40)
        assert not state.success_criteria[0].satisfied, "'looks right' with no named checks must not satisfy a criterion"
        assert state.status is not MissionStatus.COMPLETE
    finally:
        sb.cleanup()

    sb = Sandbox("judged2", with_demo_project=True)
    sb.adapter.policies["specialist"] = engineer_policy(sb.root)
    sb.adapter.policies["verify"] = lambda _req: {"status": "passed", "summary": "calc.py implements add_percent and three tests pass", "issues": [], "confidence": 0.9, "checked": ["calc.py exists and defines add_percent", "test_calc.py covers positive/zero/negative", "pytest exited 0"]}
    try:
        state = sb.runtime.new_mission("Build the feature described in REQUIREMENTS.md.", context=sb.context())
        _unparseable_criteria(sb, state)
        state = sb.runtime.run(state.mission_id, max_cycles=40)
        crit = state.success_criteria[0]
        assert crit.satisfied and crit.verification_ids, "a judgement with named checks satisfies and records"
        assert state.status is MissionStatus.COMPLETE
    finally:
        sb.cleanup()


def test_failed_judgement_marks_criterion_unsatisfied() -> None:
    sb = Sandbox("judgefail", with_demo_project=True)
    sb.adapter.policies["specialist"] = engineer_policy(sb.root)
    sb.adapter.policies["verify"] = lambda _req: {"status": "failed", "summary": "rounding is wrong for negative percentages", "issues": ["add_percent(-50) mismatch"], "confidence": 0.85, "checked": ["manual evaluation of add_percent"]}
    try:
        state = sb.runtime.new_mission("Build the feature described in REQUIREMENTS.md.", context=sb.context())
        _unparseable_criteria(sb, state)
        state = sb.runtime.run(state.mission_id, max_cycles=40)
        assert not state.success_criteria[0].satisfied
        assert state.status is not MissionStatus.COMPLETE
    finally:
        sb.cleanup()
