"""L1-L3: experience records, learned values that actually move, replay and retention.

The point of these tests is to distinguish three things the word "learning" is used for:
storing experience, changing the external agent's policy, and training model parameters. Only
the first two happen in this repository — the hosted model's weights are not touched — and these
tests assert the second one really happens rather than being a JSON field named `Q`.
"""

from __future__ import annotations

import pytest

from cogos.learning.experience import (
    ExperienceBuilder,
    accept_for_learning,
    affected_by_receipt,
    extract_features,
    quarantine,
    reward_for,
)
from cogos.learning.policy import (
    MIN_SUPPORT,
    CensoredTarget,
    ContextualBandit,
    LinearSarsaQ,
    PolicyStore,
    constrained_actions,
    dot,
)
from cogos.learning.replay import ReplayStore, Split, assign_split, learnable, retention_report
from cogos.schemas.experience import ExperienceRecord, OutcomeLabel, RewardBreakdown, StateFeatures
from cogos.schemas.mission import MissionState, SuccessCriterion, Task, TaskStatus


# == L1: experience and outcome records ======================================================


def _state() -> MissionState:
    state = MissionState(objective="Build the thing")
    state.tasks.append(Task(title="Ready one", status=TaskStatus.READY))
    state.tasks.append(Task(title="Blocked one", status=TaskStatus.BLOCKED))
    state.success_criteria.append(SuccessCriterion(description="It works"))
    return state


def test_features_are_computable_before_the_action_and_carry_no_outcome():
    state = _state()
    features = extract_features(state, "implementation")
    assert features.ready_tasks == 1 and features.blocked_tasks == 1
    assert features.task_family == "implementation"
    # No field on the schema can hold a verification result, score or post-hoc explanation.
    fields = set(StateFeatures.model_fields)
    for leak in ("verification", "test_result", "score", "label", "reward", "outcome", "holdout"):
        assert not any(leak in f for f in fields), f"state features must not carry '{leak}'"
    assert len(features.vector()) == StateFeatures.dimension()


def test_an_unknown_outcome_is_not_a_failure_and_a_timeout_is_not_a_success():
    assert reward_for(OutcomeLabel.VERIFIED_SUCCESS, 0.0, 0.0).verified_quality == 1.0
    assert reward_for(OutcomeLabel.VERIFIED_FAILURE, 0.0, 0.0).verified_quality == -1.0
    assert reward_for(OutcomeLabel.TIMEOUT, 0.0, 0.0).verified_quality < 0, "a timeout is not a success"
    assert reward_for(OutcomeLabel.BLOCKED, 0.0, 0.0).verified_quality == 0.0, "refusing to proceed past a gate is not punished"
    assert reward_for(OutcomeLabel.UNKNOWN, 0.0, 0.0).verified_quality == 0.0, "an unknown outcome is not a failure"


def test_the_reward_scales_cost_and_time_explicitly():
    r = reward_for(OutcomeLabel.VERIFIED_SUCCESS, cost_usd=1.0, elapsed_seconds=60.0)
    assert r.cost_penalty == -1.0 and r.time_penalty == -1.0
    assert r.total() == pytest.approx(1.0 * 1.0 + 0.2 * -1.0 + 0.1 * -1.0)
    # Nothing in the breakdown rewards verbosity, confidence or agent count.
    assert set(RewardBreakdown.model_fields) == {"verified_quality", "cost_penalty", "time_penalty", "quality_coefficient", "cost_coefficient", "time_coefficient"}


def test_a_record_is_eligible_only_when_its_receipts_resolve():
    state = _state()
    builder = ExperienceBuilder("msn_1", environment_version="env-1")
    rec = builder.open(state, task_family="implementation", candidate_actions=["a", "b"], chosen_action="a")
    builder.close(rec, state, label=OutcomeLabel.VERIFIED_SUCCESS, receipts=["ver_1"], terminal=True)

    ok, why = accept_for_learning(rec, resolve_receipt=lambda rid: object(), environment_version="env-1")
    assert ok, why
    bad, why = accept_for_learning(rec, resolve_receipt=lambda rid: None, environment_version="env-1")
    assert bad is False and "does not resolve" in why


def test_version_mismatches_and_undefined_labels_are_refused_but_kept():
    state = _state()
    builder = ExperienceBuilder("msn_1", environment_version="env-1")
    rec = builder.open(state, task_family="x", candidate_actions=["a"], chosen_action="a")
    builder.close(rec, state, label=OutcomeLabel.UNKNOWN, terminal=True)
    ok, why = accept_for_learning(rec, resolve_receipt=lambda rid: object(), environment_version="env-1")
    assert ok is False and "not a failure, it is unlabelled" in why

    other_env = builder.open(state, task_family="x", candidate_actions=["a"], chosen_action="a")
    builder.close(other_env, state, label=OutcomeLabel.VERIFIED_SUCCESS, receipts=["v"], terminal=True)
    ok, why = accept_for_learning(other_env, resolve_receipt=lambda rid: object(), environment_version="env-2")
    assert ok is False and "environment version mismatch" in why

    # Refusal is not deletion: the record stays, with its reason.
    quarantine(rec, why)
    assert rec.quarantined and rec.quarantine_reason


def test_an_invalidated_receipt_is_traceable_through_lineage():
    state = _state()
    builder = ExperienceBuilder("msn_1")
    a = builder.open(state, task_family="x", candidate_actions=["a"], chosen_action="a")
    builder.close(a, state, label=OutcomeLabel.VERIFIED_SUCCESS, receipts=["ver_shared"], terminal=True)
    b = builder.open(state, task_family="x", candidate_actions=["a"], chosen_action="a")
    builder.close(b, state, label=OutcomeLabel.VERIFIED_SUCCESS, receipts=["ver_other"], terminal=True)
    assert [r.id for r in affected_by_receipt([a, b], "ver_shared")] == [a.id]


def test_a_censored_episode_is_kept_but_never_bootstrapped():
    state = _state()
    builder = ExperienceBuilder("msn_1")
    rec = builder.open(state, task_family="x", candidate_actions=["a"], chosen_action="a")
    builder.close(rec, state, label=OutcomeLabel.VERIFIED_SUCCESS, receipts=["v"], truncated=True)
    assert rec.bootstrappable() is False
    assert rec.next_state_features is None


# == L2: values that move and change the next decision ========================================


def test_hard_constraints_remove_options_before_any_value_is_consulted():
    candidates = ["cheap_tool", "denied_tool", "held_tool", "ungated_tool"]
    allowed = constrained_actions(
        candidates,
        authorized={"cheap_tool", "held_tool", "ungated_tool"},
        held={"held_tool"},
        evidence_gated={"ungated_tool"},
    )
    assert allowed == ["cheap_tool"], "a denied, held or gated action is not on the menu at any value"


def test_the_bandit_uses_the_validated_baseline_until_it_has_data_support():
    bandit = ContextualBandit("retrieval", ["relevance", "recent", "failure_first"], baseline="relevance", seed=1)
    action, why = bandit.select("implementation|ready|clean|free|budget|nofail", ["relevance", "recent"])
    assert action == "relevance" and "baseline" in why


def test_bandit_parameters_move_and_change_the_next_eligible_decision():
    context = "implementation|ready|clean|free|budget|nofail"
    bandit = ContextualBandit("retrieval", ["relevance", "recent"], baseline="relevance", exploration=0.0, seed=1)
    before, _ = bandit.select(context, ["relevance", "recent"])
    assert before == "relevance"

    for _ in range(MIN_SUPPORT):
        bandit.update(context, "relevance", -0.4)
        bandit.update(context, "recent", 0.8)

    assert bandit.value(context, "recent") == pytest.approx(0.8)
    assert bandit.value(context, "relevance") == pytest.approx(-0.4)
    after, why = bandit.select(context, ["relevance", "recent"])
    assert after == "recent", "a learned estimate must actually change the next decision"
    assert "learned estimate" in why
    assert bandit.update_count == MIN_SUPPORT * 2


def test_a_hand_calculated_sarsa_transition_moves_the_parameters_exactly():
    """θ ← θ + α·δ·φ with δ = r + γ·m·Q(s',a') − Q(s,a), computed by hand.

    Both states are constructed so their feature vectors are known exactly, the initial weights
    are zero, and the arithmetic is checked term by term rather than by 'it changed'.
    """
    learner = LinearSarsaQ("impl", ["explore", "exploit"], alpha=0.5, gamma=0.9)
    s0 = StateFeatures(task_family="impl", ready_tasks=10, evidence_completeness=0.5, remaining_budget_fraction=1.0)
    s1 = StateFeatures(task_family="impl", ready_tasks=0, evidence_completeness=1.0, remaining_budget_fraction=0.5)
    phi0, phi1 = s0.vector(), s1.vector()

    rec = ExperienceRecord(
        mission_id="m",
        trajectory_id="t",
        chosen_action="explore",
        candidate_actions=["explore", "exploit"],
        state_features=s0,
        next_state_features=s1,
        reward=RewardBreakdown(verified_quality=1.0, cost_coefficient=0.0, time_coefficient=0.0),
        label=OutcomeLabel.VERIFIED_SUCCESS,
        terminal=False,
    )

    # Step 1: everything is zero, so Q(s,a) = Q(s',a') = 0 and δ = r = 1.0.
    assert learner.q(s0, "explore") == 0.0 and learner.q(s1, "exploit") == 0.0
    delta1 = learner.update(rec, next_action="exploit")
    assert delta1 == pytest.approx(1.0)
    expected_theta = [0.5 * 1.0 * g for g in phi0]
    assert learner.theta["explore"] == pytest.approx(expected_theta)
    assert learner.q(s0, "explore") == pytest.approx(dot(phi0, expected_theta))

    # Step 2: hand-compute δ from the weights step 1 produced.
    q_sa = dot(phi0, learner.theta["explore"])
    q_next = dot(phi1, learner.theta["exploit"])  # still zero: 'exploit' was never updated
    expected_delta2 = 1.0 + 0.9 * 1.0 * q_next - q_sa
    delta2 = learner.update(rec, next_action="exploit")
    assert delta2 == pytest.approx(expected_delta2)
    assert learner.theta["explore"] == pytest.approx([t + 0.5 * expected_delta2 * g for t, g in zip(expected_theta, phi0)])
    assert learner.update_count == 2


def test_a_terminal_state_does_not_bootstrap():
    learner = LinearSarsaQ("impl", ["a"], alpha=0.5, gamma=0.9)
    s0 = StateFeatures(task_family="impl", ready_tasks=4)
    rec = ExperienceRecord(
        mission_id="m", trajectory_id="t", chosen_action="a", candidate_actions=["a"],
        state_features=s0, next_state_features=None,
        reward=RewardBreakdown(verified_quality=1.0, cost_coefficient=0.0, time_coefficient=0.0),
        label=OutcomeLabel.VERIFIED_SUCCESS, terminal=True,
    )
    assert rec.mask() == 0.0
    assert learner.td_error(rec, next_action=None) == pytest.approx(1.0), "δ = r at a terminal state"


def test_a_truncated_transition_is_censored_rather_than_fabricated():
    learner = LinearSarsaQ("impl", ["a"], alpha=0.5, gamma=0.9)
    s0 = StateFeatures(task_family="impl", ready_tasks=4)
    rec = ExperienceRecord(
        mission_id="m", trajectory_id="t", chosen_action="a", candidate_actions=["a"],
        state_features=s0, next_state_features=None,
        reward=RewardBreakdown(verified_quality=0.0),
        label=OutcomeLabel.TIMEOUT, terminal=False, truncated=True,
    )
    with pytest.raises(CensoredTarget):
        learner.td_error(rec, next_action="a")


def test_a_learner_survives_serialisation_and_restart_unchanged():
    learner = LinearSarsaQ("impl", ["a", "b"], alpha=0.25, gamma=0.8, seed=7)
    s = StateFeatures(task_family="impl", ready_tasks=3)
    rec = ExperienceRecord(
        mission_id="m", trajectory_id="t", chosen_action="a", candidate_actions=["a", "b"],
        state_features=s, reward=RewardBreakdown(verified_quality=1.0), label=OutcomeLabel.VERIFIED_SUCCESS, terminal=True,
    )
    learner.update(rec, None)
    version = learner.to_version(manifest=[rec.id])
    assert version.algorithm == "sarsa/linear" and version.learning_rate == 0.25 and version.discount == 0.8
    assert version.seed == 7 and version.update_count == 1 and version.manifest_hash

    restored = LinearSarsaQ("impl", ["a", "b"])
    restored.load_version(version)
    assert restored.q(s, "a") == pytest.approx(learner.q(s, "a"))
    assert restored.alpha == 0.25 and restored.gamma == 0.8 and restored.update_count == 1


def test_policy_versions_activate_transactionally_with_a_real_rollback_target():
    from cogos.evaluation.support import Sandbox

    sb = Sandbox("l2-policy")
    try:
        store = PolicyStore(sb.runtime.store)
        first = store.save(LinearSarsaQ("impl", ["a"]).to_version())
        assert store.active("impl") is None, "a saved candidate is inert until activated"

        store.activate("impl", first.id)
        assert store.active("impl").id == first.id

        second = store.save(LinearSarsaQ("impl", ["a", "b"]).to_version())
        store.activate("impl", second.id)
        active = store.active("impl")
        assert active.id == second.id and active.rollback_to == first.id

        rolled = store.rollback("impl")
        assert rolled is not None and rolled.id == first.id
        assert store.active("impl").id == first.id
    finally:
        sb.cleanup()


# == L3: replay, splits, retention ============================================================


def _episode(trajectory: str, n: int = 3, policy_id: str = "p1") -> list[ExperienceRecord]:
    out = []
    for i in range(n):
        out.append(
            ExperienceRecord(
                mission_id="m",
                trajectory_id=trajectory,
                step_index=i,
                chosen_action="a" if i % 2 == 0 else "b",
                candidate_actions=["a", "b"],
                policy_id=policy_id,
                state_features=StateFeatures(task_family="impl", ready_tasks=i),
                reward=RewardBreakdown(verified_quality=1.0),
                label=OutcomeLabel.VERIFIED_SUCCESS,
                terminal=(i == n - 1),
                next_state_features=None if i == n - 1 else StateFeatures(task_family="impl", ready_tasks=i + 1),
            )
        )
    return out


def test_splits_are_by_task_instance_so_one_trajectory_never_straddles_them():
    store = ReplayStore()
    for name in [f"traj-{i}" for i in range(40)]:
        store.extend(_episode(name))
    for split in Split:
        trajectories = {r.trajectory_id for r in store.by_split(split)}
        for other in Split:
            if other is split:
                continue
            assert trajectories.isdisjoint({r.trajectory_id for r in store.by_split(other)})
    assert assign_split("traj-1") is assign_split("traj-1"), "assignment is stable"


def test_duplicates_are_collapsed_and_counted():
    store = ReplayStore()
    episode = _episode("traj-dup")
    assert store.extend(episode) == 3
    assert store.extend([r.model_copy(deep=True) for r in episode]) == 0
    assert store.duplicates_collapsed == 3
    assert store.stats()["records"] == 3


def test_on_policy_training_uses_only_current_policy_episodes():
    store = ReplayStore()
    store.extend(_episode("traj-new", policy_id="p2"))
    store.extend(_episode("traj-old", policy_id="p1"))
    current = [t for t in store.trajectories(Split.TRAIN, policy_id="p2")]
    for episode in current:
        assert all(r.policy_id == "p2" for r in episode)
    all_episodes = store.trajectories(Split.TRAIN)
    assert len(all_episodes) >= len(current), "older episodes are kept for diagnostics, not fed to the on-policy update"


def test_unknown_and_censored_records_are_kept_but_excluded_from_learning():
    store = ReplayStore()
    episode = _episode("traj-x")
    episode[0].label = OutcomeLabel.UNKNOWN
    episode[1].label = OutcomeLabel.CENSORED
    store.extend(episode)
    assert store.stats()["records"] == 3, "nothing is dropped"
    assert [r.label for r in learnable(store.records)] == [OutcomeLabel.VERIFIED_SUCCESS]


def test_training_an_episode_applies_updates_and_counts_censored_transitions():
    learner = LinearSarsaQ("impl", ["a", "b"], alpha=0.1, gamma=0.9)
    episode = _episode("traj-train")
    episode[1].truncated = True
    episode[1].next_state_features = None
    report = learner.learn_from_trajectory(episode)
    assert report["applied"] == 2 and report["censored"] == 1
    assert learner.update_count == 2


def test_retention_is_measured_against_the_same_prior_instances():
    prior = _episode("traj-prior")
    scores = iter([0.9, 0.5])

    def evaluate(_records):
        return next(scores)

    good = retention_report(evaluate, prior, before=0.9)
    assert good["regressed"] is False and good["prior_instances"] == 3

    bad = retention_report(evaluate, prior, before=0.9)
    assert bad["regressed"] is True and bad["delta"] < 0


def test_replay_sampling_is_tracked_so_it_is_not_called_unseen_evidence():
    from cogos.learning.replay import replayed_is_not_new_evidence

    store = ReplayStore()
    store.extend(_episode("traj-sample"))
    drawn = store.by_split(store.split_of(store.records[0]))
    store.by_split(store.split_of(store.records[0]))
    assert all(store.sampled[r.id] >= 2 for r in drawn)
    note = replayed_is_not_new_evidence(drawn)
    assert "not unseen evidence" in note["note"]


# == L2 integration: the learned policy really drives a runtime decision ======================


def test_the_learned_retrieval_policy_changes_what_the_running_loop_retrieves():
    """Not a JSON field named Q: the bandit's estimates change the runtime's next retrieval."""
    from cogos.evaluation.scenarios import engineer_policy
    from cogos.evaluation.support import Sandbox
    from cogos.learning.retrieval_policy import BASELINE, STRATEGIES, LearnedRetrieval

    sb = Sandbox("l2-integration", with_demo_project=True)
    sb.adapter.policies["specialist"] = engineer_policy(sb.root)
    try:
        retrieval: LearnedRetrieval = sb.runtime.executive.retrieval
        state = sb.runtime.new_mission("Build the feature described in REQUIREMENTS.md.", context=sb.context())
        sb.runtime.run(state.mission_id, max_cycles=40)

        assert retrieval.bandit.update_count > 0, "every retrieval feeds the policy a measured reward"
        traces = [t for t in sb.runtime.store.traces(state.mission_id, limit=5000) if t.kind == "policy"]
        assert traces, "the strategy actually used is recorded"
        assert traces[0].data["strategy"] in STRATEGIES

        # Now teach it, in the exact context the runtime uses, and check the choice flips.
        context = traces[-1].data["context"]
        other = next(s for s in STRATEGIES if s != BASELINE)
        for _ in range(5):
            retrieval.bandit.update(context, BASELINE, -1.0)
            retrieval.bandit.update(context, other, 1.0)
        chosen, why = retrieval.bandit.select(context, list(STRATEGIES))
        assert chosen == other, "a learned estimate must change the next eligible decision"
        assert "learned estimate" in why
    finally:
        sb.cleanup()


def test_the_retrieval_reward_measures_usefulness_not_volume():
    from cogos.learning.retrieval_policy import LearnedRetrieval

    r = LearnedRetrieval()
    assert r.reward(used=6, retrieved=6, quarantined=0) > r.reward(used=3, retrieved=6, quarantined=0)
    assert r.reward(used=6, retrieved=6, quarantined=0) == r.reward(used=60, retrieved=60, quarantined=0), "volume alone earns nothing"
    assert r.reward(used=5, retrieved=6, quarantined=1) < r.reward(used=6, retrieved=6, quarantined=0), "recalling poisoned content is a cost"
    assert r.reward(used=0, retrieved=0, quarantined=0) == 0.0


def test_the_learner_cannot_reach_beyond_its_closed_set_of_validated_options():
    from cogos.learning.retrieval_policy import STRATEGIES, LearnedRetrieval

    r = LearnedRetrieval()
    features = StateFeatures(task_family="impl", ready_tasks=2)
    # Even after being taught that a forbidden action is wonderful, it cannot be selected:
    # it is not in the authorized set the constraint filter builds.
    r.bandit.actions.append("shell_rm_rf")
    for _ in range(10):
        r.bandit.update(features.bucket(), "shell_rm_rf", 10.0)
    chosen, _ = r.choose(features)
    assert chosen in STRATEGIES
    assert chosen != "shell_rm_rf"
