"""L4-L6: matched interventions, research direction, prediction discipline, bounded evolution."""

from __future__ import annotations

import pytest

from cogos.evaluation.support import Sandbox
from cogos.learning.elicitation import (
    FailureClass,
    ResearchOption,
    ResearchSelector,
    diagnose,
    intervention_for,
)
from cogos.learning.evolution import (
    CandidateStatus,
    EvolutionRegistry,
    GradingContract,
    ReleaseGate,
    ReleaseScope,
    evaluate_candidate_against,
)


# == L4: elicitation ==========================================================================


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("shell disabled by policy: requires explicit human authorization", FailureClass.AUTHORIZATION_REFUSAL),
        ("'ripgrep' binary not found on PATH", FailureClass.TOOL_ENVIRONMENT),
        ("no claim relates to the criterion; insufficient evidence", FailureClass.MISSING_EVIDENCE),
        ("verification method is not machine-checkable", FailureClass.VERIFIER_WEAKNESS),
        ("the requirement is ambiguous and admits multiple interpretations", FailureClass.UNRESOLVED_AMBIGUITY),
        ("the assumption was wrong: the evidence refutes it", FailureClass.INCORRECT_PREMISE),
        ("schema mismatch: wrong units in the input column", FailureClass.REPRESENTATION),
        ("the step was too large; prerequisite dependencies missing", FailureClass.DECOMPOSITION),
        ("output truncated at max_turns before the step finished", FailureClass.REASONING_ALLOCATION),
    ],
)
def test_each_failure_class_is_diagnosed_from_its_own_signal(text, expected):
    assert diagnose(text).failure_class is expected


def test_the_intervention_is_matched_to_the_diagnosis_and_states_what_would_confirm_it():
    for klass in FailureClass:
        intervention = intervention_for(klass)
        assert intervention.failure_class is klass
        assert intervention.action and intervention.rationale
        assert intervention.tests_the_diagnosis, "an intervention that cannot be wrong tests nothing"
        assert intervention.increases_authority is False


def test_a_refusal_is_never_answered_with_a_route_around_it():
    d = diagnose("the operation was denied by policy and requires explicit human authorization")
    assert d.failure_class is FailureClass.AUTHORIZATION_REFUSAL
    action = d.intervention.action.lower()
    assert "respect the denial" in action
    for bypass in ("reword", "rephrase", "another agent", "different route", "retry as", "escalate privileges", "bypass", "work around"):
        assert bypass not in action
    # And no intervention anywhere offers one.
    for klass in FailureClass:
        text = intervention_for(klass).action.lower()
        assert "bypass" not in text and "another agent" not in text


def test_repeating_the_same_structural_failure_is_diagnosed_as_the_non_answer_it_is():
    d = diagnose("connection reset", signature="sig-a", previous_signatures=["sig-a", "sig-a"])
    assert d.failure_class is FailureClass.REPEATED_INEFFECTIVE
    assert "stop this approach" in d.intervention.action
    assert d.confidence >= 0.8


def test_more_attempts_alone_never_counts_as_an_intervention():
    d = diagnose("something went wrong in an unrecognised way", attempts=4)
    assert d.failure_class is FailureClass.REPEATED_INEFFECTIVE
    assert "further identical attempt carries no new information" in d.intervention.rationale


def test_raising_effort_is_scoped_and_compared_at_equal_budget():
    action = intervention_for(FailureClass.REASONING_ALLOCATION).action
    assert "bounded step only" in action and "equal total budget" in action


# == L4: research direction ===================================================================


def _option(name: str, **kw) -> ResearchOption:
    kw.setdefault("falsifying_experiment", f"cheap check for {name}")
    kw.setdefault("stopping_condition", "three uninformative attempts")
    return ResearchOption(hypothesis=name, **kw)


def test_the_selector_prefers_the_cheap_informative_test():
    cheap = _option("cheap", expected_information=0.6, resource_estimate=0.1)
    expensive = _option("expensive", expected_information=0.7, resource_estimate=1.0)
    selector = ResearchSelector([cheap, expensive])
    assert selector.next_option() is cheap, "expected information per unit resource, not raw expected information"


def test_a_hypothesis_with_no_way_to_be_wrong_scores_lower():
    falsifiable = _option("falsifiable", expected_information=0.5, resource_estimate=0.1)
    unfalsifiable = ResearchOption(hypothesis="unfalsifiable", expected_information=0.5, resource_estimate=0.1, falsifying_experiment="")
    assert unfalsifiable.score() < falsifiable.score()


def test_model_estimates_stay_priors_until_something_is_measured():
    option = _option("untested", expected_information=0.9)
    assert option.measured_usefulness() is None, "an estimate is not a result"
    option.attempts, option.informative_attempts = 4, 1
    assert option.measured_usefulness() == pytest.approx(0.25)
    assert option.score() < 0.9 / 0.1, "measured history pulls the score away from the prior"


def test_disconfirmation_revises_the_hypothesis_and_promotes_its_alternatives():
    option = _option("the cache is stale", plausible_alternatives=["the clock is wrong", "the writer never ran"])
    selector = ResearchSelector([option])
    before = option.expected_information
    report = selector.record_result(option, informative=True, disconfirmed=True)

    assert option.expected_information < before
    assert "promoting 2 alternative(s)" in report["note"]
    promoted = [o.hypothesis for o in selector.options if o.hypothesis != option.hypothesis]
    assert promoted == ["the clock is wrong", "the writer never ran"]


def test_an_unproductive_branch_is_stopped_rather_than_re_run():
    option = _option("dead end", expected_information=0.9)
    selector = ResearchSelector([option], max_uninformative=3)
    for _ in range(3):
        selector.record_result(option, informative=False)
    assert option.exhausted() is True
    assert selector.active() == []
    assert selector.next_option() is None, "the selector stops instead of spinning"


# == L6: bounded evolution ====================================================================


def _contract(independent: bool = True) -> GradingContract:
    return GradingContract(name="skill-release", required_checks=["adversarial", "regression"], minimum_improvement=0.05, independent=independent)


def _evaluation(contract, *, candidate_score=0.9, baseline=0.5, failed=None, executed=True, retention=0.0):
    return evaluate_candidate_against(
        contract,
        run_baseline=lambda: (baseline, ["adversarial", "regression"], []),
        run_candidate=lambda: (candidate_score, ["adversarial", "regression"], list(failed or [])),
        retention_delta=retention,
    ).model_copy(update={"executed": executed})


def test_a_useful_candidate_travels_the_whole_path_to_an_activated_release():
    sb = Sandbox("l6-good")
    try:
        registry = EvolutionRegistry(sb.runtime.store)
        contract = _contract()
        gate = ReleaseGate(contract)
        cand = registry.propose("faster-retrieval", origin_failures=["ver_1"], variant_path="/variants/faster")
        evaluation = registry.record_evaluation(cand, _evaluation(contract))

        eligible, reasons = registry.decide(cand, evaluation, gate)
        assert eligible, reasons
        manifest = registry.release(cand, evaluation, gate, source_hashes={"skill.md": "abc123"}, dependencies=["cogos"], model_configuration={"model": "claude-fable-5-1"})
        assert manifest is not None and manifest.seal()
        assert manifest.scope is ReleaseScope.LOCAL, "producing a patch is not publishing a release"
        assert registry.active_release().id == manifest.id
        assert [c.status for c in registry.candidates()] == [CandidateStatus.ACTIVE]
    finally:
        sb.cleanup()


def test_a_deliberately_defective_candidate_is_rejected_and_never_activated():
    sb = Sandbox("l6-bad")
    try:
        registry = EvolutionRegistry(sb.runtime.store)
        contract = _contract()
        gate = ReleaseGate(contract)
        cand = registry.propose("regressing-retrieval", origin_failures=["ver_2"], variant_path="/variants/bad")
        evaluation = registry.record_evaluation(cand, _evaluation(contract, candidate_score=0.51, failed=["regression"], retention=-0.2))

        eligible, reasons = registry.decide(cand, evaluation, gate)
        assert eligible is False
        assert any("failed checks" in r for r in reasons)
        assert any("below the declared minimum" in r for r in reasons)
        assert any("retention regressed" in r for r in reasons)
        assert registry.release(cand, evaluation, gate) is None
        assert registry.active_release() is None
    finally:
        sb.cleanup()


def test_a_candidate_cannot_redefine_success_to_make_itself_look_better():
    sb = Sandbox("l6-tamper")
    try:
        registry = EvolutionRegistry(sb.runtime.store)
        contract = _contract()
        gate = ReleaseGate(contract)
        cand = registry.propose("sneaky", origin_failures=["ver_3"])
        evaluation = registry.record_evaluation(cand, _evaluation(contract, candidate_score=0.51))

        # The candidate lowers the bar after being graded.
        contract.minimum_improvement = 0.0
        contract.version += 1
        eligible, reasons = registry.decide(cand, evaluation, ReleaseGate(contract))
        assert eligible is False
        assert any("grading contract changed" in r for r in reasons)
    finally:
        sb.cleanup()


def test_a_score_with_no_execution_behind_it_cannot_be_released():
    sb = Sandbox("l6-unrun")
    try:
        registry = EvolutionRegistry(sb.runtime.store)
        contract = _contract()
        cand = registry.propose("unrun", origin_failures=["ver_4"])
        evaluation = registry.record_evaluation(cand, _evaluation(contract, executed=False))
        eligible, reasons = registry.decide(cand, evaluation, ReleaseGate(contract))
        assert eligible is False and any("never executed" in r for r in reasons)
    finally:
        sb.cleanup()


def test_a_developer_visible_holdout_keeps_promotion_blocked_and_says_so():
    sb = Sandbox("l6-shadow")
    try:
        registry = EvolutionRegistry(sb.runtime.store)
        contract = _contract(independent=False)
        gate = ReleaseGate(contract)
        cand = registry.propose("good-but-ungated", origin_failures=["ver_5"], variant_path="/v")
        evaluation = registry.record_evaluation(cand, _evaluation(contract))

        eligible, reasons = registry.decide(cand, evaluation, gate)
        assert eligible is False, "the local path is complete; production promotion is not"
        assert cand.status is CandidateStatus.SHADOW
        assert any("development fixture rather than an independent benchmark" in r for r in reasons)
        assert registry.release(cand, evaluation, gate) is None
    finally:
        sb.cleanup()


def test_rollback_returns_to_the_previous_release():
    sb = Sandbox("l6-rollback")
    try:
        registry = EvolutionRegistry(sb.runtime.store)
        contract = _contract()
        gate = ReleaseGate(contract)

        first = registry.propose("v1", origin_failures=["a"], variant_path="/v1")
        e1 = registry.record_evaluation(first, _evaluation(contract))
        registry.decide(first, e1, gate)
        m1 = registry.release(first, e1, gate)

        second = registry.propose("v2", origin_failures=["b"], variant_path="/v2")
        e2 = registry.record_evaluation(second, _evaluation(contract, candidate_score=0.95))
        registry.decide(second, e2, gate)
        m2 = registry.release(second, e2, gate)
        assert m2 is not None and m2.rollback_to == m1.id

        restored = registry.rollback("regression observed after activation")
        assert restored is not None and restored.id == m1.id
        assert registry.active_release().id == m1.id
        rolled = [c for c in registry.candidates() if c.name == "v2"][0]
        assert rolled.status is CandidateStatus.ROLLED_BACK
        assert rolled.reasons == ["regression observed after activation"]
    finally:
        sb.cleanup()


# == L5: prediction discipline ================================================================


def test_a_prediction_is_logged_before_its_outcome_and_a_wrong_one_does_not_rewrite_history():
    from cogos.schemas.world import WorldModel
    from cogos.world_model import WorldModelManager

    world = WorldModel()
    manager = WorldModelManager(world)
    pred = manager.add_prediction("the suite will pass after the fix", probability=0.8)
    assert pred.made_at and pred.resolved is None and pred.resolved_at is None
    assert pred.stated_probability == 0.8

    resolved = manager.resolve_prediction(pred.id, outcome=False, note="two tests still fail")
    assert resolved is not None and resolved.resolved is False
    assert resolved.resolved_at is not None and resolved.resolved_at >= resolved.made_at
    assert resolved.stated_probability == 0.8, "the prediction is not edited to match what happened"
    assert resolved.probability == 0.8
    assert "two tests still fail" in resolved.outcome


def test_consolidation_preserves_contradictory_evidence():
    sb = Sandbox("l5-consolidate")
    try:
        from cogos.schemas.memory import MemoryClass

        sb.runtime.memory.remember(MemoryClass.SEMANTIC, "The market is growing at 5% per year", tags=["market"], mission_id="m1", confidence=0.8, importance=0.7)
        sb.runtime.memory.remember(MemoryClass.SEMANTIC, "The market is shrinking by 2% per year", tags=["market"], mission_id="m1", confidence=0.7, importance=0.7)
        before = sb.runtime.memory.stats()

        sb.runtime.memory.consolidate(mission_id="m1")
        after = sb.runtime.memory.stats()

        found = " ".join(m.content for m in sb.runtime.memory.retrieve("market growth rate per year", limit=10))
        assert "growing" in found and "shrinking" in found, "consolidation must not erase inconvenient evidence"
        assert after.get("semantic", 0) >= before.get("semantic", 0) - 1
    finally:
        sb.cleanup()
