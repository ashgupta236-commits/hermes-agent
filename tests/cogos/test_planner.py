"""Tests for the long-horizon planner (cogos.planner.dag.Planner)."""

from __future__ import annotations

import json

from cogos.planner import Planner, RetryDecision
from cogos.schemas.cognition import GoalSpec, TaskSpec
from cogos.schemas.mission import MissionState, SuccessCriterion, Task, TaskStatus, Unknown


def _state(**kw) -> MissionState:
    return MissionState(objective="plan things", **kw)


def _chain(state: MissionState) -> tuple[Planner, dict[str, str]]:
    """a -> b -> c plus d (depends on a and b)."""
    planner = Planner(state)
    ids = planner.instantiate_from_specs(
        [GoalSpec(key="g1", title="Root goal"), GoalSpec(key="g2", title="Child goal", level="milestone", parent_key="g1")],
        [
            TaskSpec(key="a", title="Task A", goal_key="g1"),
            TaskSpec(key="b", title="Task B", goal_key="g2", depends_on=["a"]),
            TaskSpec(key="c", title="Task C", goal_key="g2", depends_on=["b"]),
            TaskSpec(key="d", title="Task D", depends_on=["a", "b"]),
        ],
    )
    return planner, ids


# --- construction -------------------------------------------------------------------


def test_instantiate_from_specs_maps_keys_deps_and_goals():
    state = _state(
        unknowns=[Unknown(question="What is the root cause of the failure?"), Unknown(question="Which vendor is cheapest?")],
        success_criteria=[SuccessCriterion(description="The full test suite passes"), SuccessCriterion(description="Docs updated")],
    )
    planner = Planner(state)
    ids = planner.instantiate_from_specs(
        [GoalSpec(key="g1", title="Root", rationale="why"), GoalSpec(key="g2", title="Child", level="milestone", parent_key="g1"), GoalSpec(key="g3", title="Orphan", parent_key="missing")],
        [
            TaskSpec(key="a", title="Repro", goal_key="g1", priority=1.7, parameters_json=json.dumps({"tool": "shell"}), resolves_unknowns=["root cause of the failure"], addresses_criteria=["full test suite passes"]),
            TaskSpec(key="b", title="Fix", goal_key="g2", depends_on=["a", "ghost", "b"], parallel_safe=False, resolves_unknowns=[state.unknowns[1].id]),
            TaskSpec(key="c", title="Unlinked", goal_key="nope", priority=-2),
        ],
    )
    assert set(ids) == {"a", "b", "c"}
    assert {t.id for t in state.tasks} == set(ids.values())

    goals = {g.title: g for g in state.goals}
    assert goals["Root"].rationale == "why" and goals["Root"].parent_id is None
    assert goals["Child"].parent_id == goals["Root"].id and goals["Child"].level == "milestone"
    assert goals["Orphan"].parent_id is None

    a, b, c = (state.task(ids[k]) for k in "abc")
    assert a.goal_id == goals["Root"].id and b.goal_id == goals["Child"].id and c.goal_id is None
    assert a.priority == 1.0 and c.priority == 0.0  # clamped
    assert a.parameters["tool"] == "shell"
    assert a.parameters["_resolves_unknowns"] == ["root cause of the failure"]
    # dependency resolution: unknown keys dropped, self-reference dropped
    assert b.depends_on == [a.id]
    assert b.parallel_safe is False
    # linking by text similarity (a) and by id (b)
    assert a.resolves_unknown_ids == [state.unknowns[0].id]
    assert a.addresses_criterion_ids == [state.success_criteria[0].id]
    assert b.resolves_unknown_ids == [state.unknowns[1].id]
    assert c.resolves_unknown_ids == [] and c.addresses_criterion_ids == []
    # readiness computed on instantiation
    assert a.status is TaskStatus.READY and c.status is TaskStatus.READY and b.status is TaskStatus.PENDING


# --- readiness ----------------------------------------------------------------------------


def test_compute_ready_transitions_pending_to_ready_when_deps_done():
    state = _state()
    planner, ids = _chain(state)
    a, b, c, d = (state.task(ids[k]) for k in "abcd")
    assert [t.id for t in planner.compute_ready()] == [a.id]
    assert (b.status, c.status, d.status) == (TaskStatus.PENDING,) * 3

    a.status = TaskStatus.DONE
    ready = planner.compute_ready()
    assert {t.id for t in ready} == {b.id}
    assert b.status is TaskStatus.READY and d.status is TaskStatus.PENDING  # d still waits on b

    b.status = TaskStatus.DONE
    assert {t.id for t in planner.compute_ready()} == {c.id, d.id}

    # a READY task whose prerequisite regresses falls back to PENDING
    b.status = TaskStatus.ACTIVE
    planner.compute_ready()
    assert c.status is TaskStatus.PENDING and d.status is TaskStatus.PENDING


def test_compute_ready_blocks_when_dep_failed_permanently():
    state = _state()
    planner, ids = _chain(state)
    a, b, c, d = (state.task(ids[k]) for k in "abcd")
    a.status = TaskStatus.DONE
    b.status = TaskStatus.FAILED
    b.attempts = 1
    planner.compute_ready()
    # a retryable failure is not permanent: dependants simply wait
    assert c.status is TaskStatus.PENDING and d.status is TaskStatus.PENDING

    b.attempts = b.max_attempts
    ready = planner.compute_ready()
    assert c.status is TaskStatus.BLOCKED and d.status is TaskStatus.BLOCKED
    assert c.failure_reason == "prerequisite failed permanently"
    assert ready == []
    assert planner.is_plan_exhausted() is True  # blocked/failed/done: nothing is live

    # once b succeeds after all, blocked dependants become ready again
    b.status = TaskStatus.DONE
    assert {t.id for t in planner.compute_ready()} == {c.id, d.id}
    assert c.failure_reason == "prerequisite failed permanently"  # reason is historical, not cleared


def test_compute_ready_propagates_cancellation():
    state = _state()
    planner, ids = _chain(state)
    a, b, c, d = (state.task(ids[k]) for k in "abcd")
    a.status = TaskStatus.CANCELLED
    planner.compute_ready()
    assert b.status is TaskStatus.CANCELLED and d.status is TaskStatus.CANCELLED
    # c depends on b, which is now cancelled: a second pass cascades
    planner.compute_ready()
    assert c.status is TaskStatus.CANCELLED
    assert planner.is_plan_exhausted() is True


# --- ordering -------------------------------------------------------------------------------


def test_score_prefers_task_resolving_high_priority_unknown():
    unk = Unknown(question="Which database?", decision_importance=1.0, probability_changes_decision=1.0, expected_information_gain=1.0, estimated_cost=0.1)
    state = _state(unknowns=[unk])
    planner = Planner(state)
    ids = planner.instantiate_from_specs([], [
        TaskSpec(key="plain", title="Plain task", priority=0.5),
        TaskSpec(key="resolver", title="Resolve the database unknown", priority=0.5, resolves_unknowns=[unk.id]),
    ])
    plain, resolver = state.task(ids["plain"]), state.task(ids["resolver"])
    assert unk.priority() == 10.0
    assert planner.score(resolver) > planner.score(plain)
    assert abs(planner.score(resolver) - (0.5 + 0.35)) < 1e-9
    assert planner.score(plain) == 0.5
    assert [t.id for t in planner.next_tasks()] == [resolver.id, plain.id]

    # once the unknown is resolved the bonus disappears
    unk.resolved = True
    assert planner.score(resolver) == planner.score(plain)


def test_score_rewards_criteria_verification_dependents_and_penalises_attempts():
    crit = SuccessCriterion(description="Feature works")
    state = _state(success_criteria=[crit])
    planner = Planner(state)
    ids = planner.instantiate_from_specs([], [
        TaskSpec(key="base", title="Base", priority=0.5),
        TaskSpec(key="crit", title="Advances criterion", priority=0.5, addresses_criteria=[crit.id]),
        TaskSpec(key="ver", title="Verify", priority=0.5, operation_hint="verify"),
        TaskSpec(key="dep1", title="Downstream 1", priority=0.5, depends_on=["base"]),
        TaskSpec(key="dep2", title="Downstream 2", priority=0.5, depends_on=["base"]),
    ])
    base = state.task(ids["base"])
    assert abs(planner.score(base) - (0.5 + 0.05 * 2)) < 1e-9
    assert abs(planner.score(state.task(ids["crit"])) - 0.7) < 1e-9
    assert abs(planner.score(state.task(ids["ver"])) - 0.65) < 1e-9
    base.attempts = 2
    assert abs(planner.score(base) - (0.6 - 0.2)) < 1e-9
    crit.satisfied = True
    assert planner.score(state.task(ids["crit"])) == 0.5


def test_parallel_batch_respects_parallel_safe_and_limit():
    state = _state()
    planner = Planner(state)
    ids = planner.instantiate_from_specs([], [
        TaskSpec(key="s1", title="Safe 1", priority=0.9),
        TaskSpec(key="u", title="Unsafe", priority=1.0, parallel_safe=False),
        TaskSpec(key="s2", title="Safe 2", priority=0.8),
        TaskSpec(key="s3", title="Safe 3", priority=0.7),
        TaskSpec(key="later", title="Later", priority=1.0, depends_on=["s1"]),
    ])
    batch = planner.parallel_batch(limit=4)
    assert [t.id for t in batch] == [ids["s1"], ids["s2"], ids["s3"]]
    assert all(t.parallel_safe for t in batch)
    assert [t.id for t in planner.parallel_batch(limit=2)] == [ids["s1"], ids["s2"]]
    # the unsafe task is still surfaced by next_tasks, ahead of the rest
    assert planner.next_tasks()[0].id == ids["u"]


# --- structure ---------------------------------------------------------------------------------


def test_dag_valid_detects_cycle_and_break_cycles_repairs():
    state = _state()
    planner, ids = _chain(state)
    ok, msg = planner.dag_valid()
    assert ok is True and msg == "ok"

    a, c = state.task(ids["a"]), state.task(ids["c"])
    a.depends_on.append(c.id)  # a -> c -> b -> a
    ok, msg = planner.dag_valid()
    assert ok is False
    assert msg.startswith("cycle: ")
    assert all(tid in msg for tid in (a.id, c.id, state.task(ids["b"]).id))

    removed = planner.break_cycles()
    assert removed >= 1
    assert planner.dag_valid() == (True, "ok")
    assert planner.break_cycles() == 0
    # exactly one edge was removed and the plan is runnable again
    assert sum(len(t.depends_on) for t in state.tasks) == 4
    assert len(planner.compute_ready()) >= 1


def test_dag_valid_ignores_dangling_dependency_ids():
    state = _state(tasks=[Task(title="x", depends_on=["task_missing"])])
    assert Planner(state).dag_valid() == (True, "ok")


def test_prune_irrelevant_lowers_priority_of_unlinked_tasks():
    crit = SuccessCriterion(description="c")
    state = _state(success_criteria=[crit])
    planner = Planner(state)
    ids = planner.instantiate_from_specs([GoalSpec(key="g", title="G")], [
        TaskSpec(key="orphan", title="Orphan busywork", priority=0.9),
        TaskSpec(key="goal", title="Linked to goal", goal_key="g", priority=0.9),
        TaskSpec(key="crit", title="Linked to criterion", priority=0.9, addresses_criteria=[crit.id]),
        TaskSpec(key="verify", title="Verify things", priority=0.9, operation_hint="verify"),
        TaskSpec(key="low", title="Already low", priority=0.1),
        TaskSpec(key="done", title="Done orphan", priority=0.9),
    ])
    state.task(ids["done"]).status = TaskStatus.DONE
    flagged = planner.prune_irrelevant()
    assert {t.id for t in flagged} == {ids["orphan"], ids["low"]}
    assert state.task(ids["orphan"]).priority == 0.2
    assert state.task(ids["low"]).priority == 0.1  # min(priority, 0.2)
    for k in ("goal", "crit", "verify", "done"):
        assert state.task(ids[k]).priority == 0.9


# --- progress -----------------------------------------------------------------------------------


def test_progress_mixes_tasks_and_criteria():
    state = _state()
    planner = Planner(state)
    assert planner.progress() == 0.0

    state.tasks = [Task(title="a", status=TaskStatus.DONE), Task(title="b"), Task(title="cancelled", status=TaskStatus.CANCELLED)]
    assert planner.progress() == 0.5  # no criteria: criterion part mirrors task part (1/2)

    state.success_criteria = [SuccessCriterion(description="x", satisfied=True), SuccessCriterion(description="y"), SuccessCriterion(description="z"), SuccessCriterion(description="w")]
    assert planner.progress() == round(0.5 * 0.5 + 0.5 * 0.25, 3)

    for t in state.tasks:
        if t.status is not TaskStatus.CANCELLED:
            t.status = TaskStatus.DONE
    for c in state.success_criteria:
        c.satisfied = True
    assert planner.progress() == 1.0


# --- failure policy -----------------------------------------------------------------------------


def test_failure_signature_normalises_whitespace_and_case():
    t = Task(title="t", operation_hint="execute_code")
    sig = Planner.failure_signature(t, "Connection   RESET by peer\n", "transient")
    assert sig == Planner.failure_signature(t, "connection reset by peer", "transient")
    assert sig != Planner.failure_signature(t, "connection reset by peer", "structural")
    assert len(sig) == 16


def test_retry_decision_transient_retries_with_backoff():
    planner = Planner(_state())
    t = Task(title="t", attempts=0, max_attempts=20)
    d = planner.retry_decision(t, "503 upstream", "transient")
    assert isinstance(d, RetryDecision)
    assert (d.action, d.backoff_seconds) == ("retry", 1.0)
    t.attempts = 2
    assert planner.retry_decision(t, "503 upstream", "transient").backoff_seconds == 4.0
    t.attempts = 10
    assert planner.retry_decision(t, "503 upstream", "transient").backoff_seconds == 30.0
    assert t.failure_signature == Planner.failure_signature(t, "503 upstream", "transient")


def test_retry_decision_repeated_structural_signature_replans():
    planner = Planner(_state())
    t = Task(title="t", attempts=1)
    first = planner.retry_decision(t, "AssertionError: 1 != 2", "structural")
    assert first.action == "replan" and "modify approach" in first.reason
    second = planner.retry_decision(t, "assertionerror: 1 != 2", "structural")
    assert second.action == "replan" and "identical" in second.reason
    # a different structural failure resets to the generic replan
    third = planner.retry_decision(t, "ImportError: no module", "structural")
    assert "identical" not in third.reason


def test_retry_decision_denied_escalates_and_max_attempts_abandons():
    planner = Planner(_state())
    t = Task(title="t", attempts=3, max_attempts=3)
    assert planner.retry_decision(t, "policy denied", "denied").action == "escalate"
    assert planner.retry_decision(t, "needs human", "requires_human").action == "escalate"
    # transient failures no longer retry once attempts are exhausted
    d = planner.retry_decision(t, "timeout", "transient")
    assert d.action == "abandon" and d.reason == "max attempts reached"
    d = planner.retry_decision(t, "TypeError", "structural")
    assert d.action == "abandon"
    # ... but an identical repeated signature still asks for a replan first
    assert planner.retry_decision(t, "TypeError", "structural").action == "replan"


# --- incremental planning ---------------------------------------------------------------------------


def test_add_tasks_from_specs_dedupes_by_title_and_resolves_deps():
    state = _state()
    planner, ids = _chain(state)
    goal_id = state.goals[0].id
    existing_a = state.task(ids["a"])
    before = len(state.tasks)

    created = planner.add_tasks_from_specs([
        TaskSpec(key="dup", title="  task a "),  # dedupes against "Task A" (case/whitespace-insensitive)
        TaskSpec(key="new1", title="New 1", depends_on=["dup"], goal_key="Root goal"),
        TaskSpec(key="new2", title="New 2", depends_on=[existing_a.id, "new1", "ghost", "new2"], goal_key=goal_id),
        TaskSpec(key="new3", title="New 3", goal_key="Unknown goal"),
    ])
    assert [t.title for t in created] == ["New 1", "New 2", "New 3"]
    assert len(state.tasks) == before + 3
    assert sum(1 for t in state.tasks if t.title.strip().lower() == "task a") == 1

    new1, new2, new3 = created
    assert new1.depends_on == [existing_a.id]  # via the deduped key
    assert new1.goal_id == goal_id  # goal resolved by title
    assert new2.goal_id == goal_id  # goal resolved by id
    assert sorted(new2.depends_on) == sorted({existing_a.id, new1.id})  # by id, by key; ghost/self dropped
    assert new3.goal_id is None
    assert new3.status is TaskStatus.READY and new1.status is TaskStatus.PENDING
    assert planner.dag_valid() == (True, "ok")
