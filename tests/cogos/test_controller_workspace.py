"""Tests for the meta-cognitive controller and the global workspace digest."""

from __future__ import annotations

from cogos.executive.controller import Assessment, MetaCognitiveController
from cogos.schemas.beliefs import Contradiction, Hypothesis
from cogos.schemas.common import ActionClass
from cogos.schemas.decisions import Decision
from cogos.schemas.mission import (
    BlockedOperation,
    Commitment,
    Goal,
    HumanRequest,
    Lesson,
    MissionState,
    Risk,
    SuccessCriterion,
    Task,
    TaskStatus,
    Unknown,
)
from cogos.workspace.global_workspace import GlobalWorkspace, WorkspaceView


def _state(**kw) -> MissionState:
    """A calm baseline: confident, non-novel, low stakes, no contradictions."""
    base = dict(objective="Assess things", confidence=0.9, success_criteria=[SuccessCriterion(description="done")], tasks=[Task(title="t1", status=TaskStatus.DONE)])
    base.update(kw)
    state = MissionState(**base)
    state.usage.cycles = 10
    return state


def _directives(state: MissionState, **kw) -> list[str]:
    return MetaCognitiveController().assess(state, **kw).directives


def _kinds(directives: list[str]) -> list[str]:
    return [d.split(":", 1)[0] for d in directives]


# --- controller ------------------------------------------------------------------------------


def test_baseline_assessment_is_quiet():
    state = _state()
    a = MetaCognitiveController().assess(state)
    assert isinstance(a, Assessment)
    assert a.directives == [] and a.effort == "high" and a.notes == []
    assert a.progress == 0.0 and a.stalled_cycles == 0
    assert a.stakes == 0.4 and a.contradiction_level == 0.0
    assert a.novelty == 0.2 and a.confidence == 0.9
    assert a.uncertainty == 0.5 and a.evidence_coverage == 0.5  # no unknowns at all
    assert a.budget_pressure == round(10 / 200, 3)
    assert state.resources["controller"]["history"][-1]["cycle"] == 10


def test_directive_for_verification_pending():
    d = _directives(_state(), verification_pending=["task_a", "task_b"])
    assert d == ["must_verify:2 completed task(s) have unverified outputs"]
    assert _directives(_state(), verification_pending=[]) == []


def test_serious_contradiction_requires_falsification_and_max_effort():
    state = _state(contradictions=[Contradiction(claim_ids=["c1", "c2"], description="sources disagree", severity=0.6)])
    a = MetaCognitiveController().assess(state, falsification_target={"statement": "X", "priority": 0.2})
    assert "must_falsify:serious contradiction — run targeted falsification before relying on the leading belief" in a.directives
    assert a.contradiction_level == 0.6 and a.effort == "max"
    # a resolved contradiction no longer counts
    state.contradictions[0].resolved = True
    a = MetaCognitiveController().assess(state, falsification_target={"statement": "X", "priority": 0.9})
    assert "should_falsify:high-stakes leading belief has not been challenged" in a.directives
    assert not any(d.startswith("must_falsify") for d in a.directives)
    assert a.effort == "high"
    # an already-attempted target is not re-flagged; a target without a directive is silent
    assert not any("falsify" in d for d in _directives(state, falsification_target={"priority": 0.9, "attempted": True}))
    assert not any("falsify" in d for d in _directives(_state(contradictions=state.contradictions)))
    # contradiction severities accumulate and are capped
    many = _state(contradictions=[Contradiction(claim_ids=["a"], description="d", severity=0.7), Contradiction(claim_ids=["b"], description="d", severity=0.7)])
    assert MetaCognitiveController().assess(many).contradiction_level == 1.0


def test_stall_detection_after_repeated_calls_without_progress():
    ctl = MetaCognitiveController(stall_threshold=3)
    state = _state()
    stalled = []
    for _ in range(4):
        a = ctl.assess(state)
        stalled.append(a.stalled_cycles)
    assert stalled == [0, 1, 2, 3]
    assert "change_strategy:progress stalled or repeated structural failures; abandon the failing approach" in a.directives
    assert a.p_strategy_wrong == round(0.1 + 0.1, 3)
    # a fresh controller with the default threshold (4) reads the persisted history: 3 stalled cycles -> 4 now
    assert any(d.startswith("change_strategy") for d in _directives(state))
    # progress resets the stall counter
    state.tasks.append(Task(title="t2", status=TaskStatus.DONE))
    a = ctl.assess(state)
    assert a.stalled_cycles == 0 and not any(d.startswith("change_strategy") for d in a.directives)
    # a progress increase alone also resets it
    ctl.assess(state)
    state.progress = 0.3
    a = ctl.assess(state)
    assert a.stalled_cycles == 0 and a.progress_rate == 0.3


def test_repeated_structural_failures_trigger_change_strategy():
    state = _state(tasks=[Task(title="f1", status=TaskStatus.FAILED, attempts=2), Task(title="f2", status=TaskStatus.FAILED, attempts=2)])
    a = MetaCognitiveController().assess(state)
    assert any(d.startswith("change_strategy") for d in a.directives)
    assert a.p_strategy_wrong == 0.4


def test_terminate_branch_for_exhausted_task():
    exhausted = Task(title="dead", status=TaskStatus.FAILED, attempts=3, max_attempts=3)
    retryable = Task(title="alive", status=TaskStatus.FAILED, attempts=1, max_attempts=3)
    d = _directives(_state(tasks=[exhausted, retryable, Task(title="ok", status=TaskStatus.DONE)]))
    assert f"terminate_branch:{exhausted.id} exhausted attempts" in d
    assert not any(retryable.id in x for x in d)


def test_escalate_when_human_request_pending_and_no_ready_tasks():
    hr = HumanRequest(kind="decision", question="Which vendor?", why_not_inferable="commercial preference")
    state = _state(human_requests=[hr], tasks=[Task(title="done", status=TaskStatus.DONE)])
    d = _directives(state)
    assert d == ["escalate:no independent work remains; a human decision is required"]
    # independent work still available: no escalation
    state.tasks.append(Task(title="pending", status=TaskStatus.PENDING))
    assert _directives(state) == []
    state.tasks[-1].status = TaskStatus.READY
    assert _directives(state) == []
    # answered requests do not escalate
    hr.answered = True
    state.tasks.pop()
    assert _directives(state) == []


def test_stop_directive_and_budget_pressure_when_over_budget():
    state = _state()
    a = MetaCognitiveController().assess(state, over_budget="max_cycles", verification_pending=["t"])
    assert a.directives[0] == "stop:max_cycles"
    assert a.budget_pressure == 1.0
    assert "must_verify:1 completed task(s) have unverified outputs" in a.directives
    # budget pressure otherwise reflects the tighter of cycles/model calls
    state.usage.model_calls = 300
    assert MetaCognitiveController().assess(state).budget_pressure == 0.75


def test_effort_max_under_high_stakes_or_novelty():
    state = _state(risks=[Risk(description="data loss", probability=1.0, impact=0.8)])
    a = MetaCognitiveController().assess(state)
    assert a.stakes == 0.8 and a.effort == "max"
    assert a.notes == ["high stakes: verification depth and independent challenge required"]
    # moderate stakes stays at high effort
    a = MetaCognitiveController().assess(_state(risks=[Risk(description="minor", probability=0.5, impact=0.5)]))
    assert a.stakes == 0.4 and a.effort == "high"
    # a novel mission with low confidence also warrants max effort
    fresh = MissionState(objective="new", confidence=0.1)
    a = MetaCognitiveController().assess(fresh)
    assert a.novelty == 0.7 and a.effort == "max" and a.notes == []


def test_challenge_blocked_unknown_and_diminishing_returns_directives():
    unk = Unknown(question="critical?", decision_importance=1.0, probability_changes_decision=1.0, expected_information_gain=1.0, estimated_cost=0.5)
    state = _state(progress=0.7, risks=[Risk(description="rollout", probability=0.8, impact=0.8)], unknowns=[unk, Unknown(question="minor", resolved=True)], blocked_operations=[BlockedOperation(operation="rm -rf", action_class=ActionClass.DESTRUCTIVE, reason="gated", what_would_unblock="grant")])
    kinds = _kinds(_directives(state, tool_results_history=[False, False, False, True]))
    assert "should_challenge" in kinds and "isolate_blocked" in kinds and "prioritise_unknowns" in kinds and "tool_unreliable" in kinds
    assert "should_challenge" not in _kinds(_directives(state, challenged=True))
    assert MetaCognitiveController().assess(state).stakes == 0.64
    a = MetaCognitiveController().assess(state, tool_results_history=[True, True, False], memory_hits=(1, 4))
    assert a.tool_reliability == round(2 / 3, 3) and a.memory_reliability == 0.25
    assert "tool_unreliable" not in _kinds(a.directives)  # fewer than 4 samples
    assert a.uncertainty == 0.5 and a.evidence_coverage == 0.5

    done = _state(progress=0.95, unknowns=[Unknown(question="q", resolved=True)])
    assert "diminishing_returns" in _kinds(_directives(done))


def test_history_is_bounded_to_50():
    ctl = MetaCognitiveController()
    state = _state()
    for i in range(60):
        state.usage.cycles = i
        ctl.assess(state)
    history = state.resources["controller"]["history"]
    assert len(history) == 50
    assert [h["cycle"] for h in history] == list(range(10, 60))
    assert set(history[0]) == {"cycle", "progress", "completed", "stalled_cycles", "uncertainty", "contradiction", "p_wrong"}


# --- workspace --------------------------------------------------------------------------------


def test_workspace_sections_present():
    unk = Unknown(question="What is the load?", decision_importance=0.9)
    state = MissionState(
        objective="Ship it",
        executive_model="claude-fable-5-1",
        explicit_constraints=["no network"],
        inferred_constraints=["keep diff small"],
        success_criteria=[SuccessCriterion(description="tests pass", verification_method="pytest", satisfied=True), SuccessCriterion(description="docs")],
        unknowns=[unk],
        hypotheses=[Hypothesis(question="q", statement="H1", status="leading", confidence=0.7), Hypothesis(question="q", statement="H2", status="eliminated")],
        contradictions=[Contradiction(claim_ids=["a"], description="disagree", severity=0.4)],
        blocked_operations=[BlockedOperation(operation="git push", action_class=ActionClass.CONSEQUENTIAL_SHARED, reason="gated", what_would_unblock="grant")],
        human_requests=[HumanRequest(kind="decision", question="Which?", why_not_inferable="taste")],
        commitments=[Commitment(statement="report by Friday")],
        decisions=[Decision(objective="o", selected_option="A", concise_rationale="r", confidence=0.6)],
        learned_lessons=[Lesson(statement="cache the index", category="tool")],
        synthesis={"conclusion": "so far so good", "confidence": 0.5, "ignored": "x"},
    )
    state.goals.append(Goal(title="Main goal"))
    ready = Task(title="Ready one", description="desc", operation_hint="verify", priority=0.8)
    active = Task(title="Active one", status=TaskStatus.ACTIVE)
    failed = Task(title="Failed one", status=TaskStatus.FAILED, attempts=2, failure_reason="boom")
    state.tasks = [ready, active, failed]

    view = GlobalWorkspace().build(
        state,
        ready_tasks=[ready],
        belief_lines=["claim A 0.9"],
        world_lines=["Acme: hq=Paris"],
        memory_lines=["remembered"],
        recent_observations=["obs 1"],
        assessment={"stakes": 0.4},
        directives=["must_verify:1"],
        tools_text="- read_file(path)\n- shell(command)",
        skills_text="- triage",
    )
    assert isinstance(view, WorkspaceView)
    expected = {
        "mission", "constraints", "success_criteria", "goals", "ready_tasks", "active_tasks", "failed_tasks", "important_unknowns",
        "high_impact_beliefs", "active_hypotheses", "contradictions", "world_model", "blocked_operations", "pending_human_requests",
        "commitments", "recent_decisions", "relevant_memory", "recent_observations", "lessons", "assessment", "controller_directives",
        "current_synthesis", "relevant_skills", "available_tools",
    }
    assert set(view.sections) == expected
    assert view.truncated is False and view.char_count == len(view.text)
    assert view.sections["mission"][1] == "objective: Ship it"
    assert "executive_model=claude-fable-5-1" in view.sections["mission"][2]
    assert view.sections["constraints"] == ["[explicit] no network", "[inferred] keep diff small"]
    assert view.sections["success_criteria"][0].startswith("[x] ") and view.sections["success_criteria"][1].startswith("[ ] ")
    assert view.sections["ready_tasks"] == [f"{ready.id} p=0.80 attempts=0 hint=verify: Ready one — desc"]
    assert view.sections["active_tasks"] == [f"{active.id}: Active one"]
    assert view.sections["failed_tasks"] == [f"{failed.id} attempts=2: Failed one — boom"]
    assert view.sections["important_unknowns"][0].startswith(f"{unk.id} prio=")
    assert len(view.sections["active_hypotheses"]) == 1 and "[leading 0.70] H1" in view.sections["active_hypotheses"][0]
    assert view.sections["blocked_operations"][0].endswith("unblock: grant")
    assert view.sections["current_synthesis"] == ["conclusion: so far so good", "confidence: 0.5"]
    assert view.sections["available_tools"] == ["- read_file(path)", "- shell(command)"]
    assert view.sections["controller_directives"] == ["must_verify:1"]
    for name in expected:
        assert f"## {name}\n" in view.text
    assert "- objective: Ship it" in view.text


def test_workspace_omits_empty_optional_sections():
    view = GlobalWorkspace().build(MissionState(objective="bare"))
    assert set(view.sections) == {"mission", "success_criteria"}
    assert view.sections["success_criteria"] == []
    assert view.text.startswith("## mission\n- id=")


def test_workspace_bounded_by_max_chars_with_truncated_flag():
    state = MissionState(objective="x" * 500, success_criteria=[SuccessCriterion(description="c" * 300) for _ in range(10)])
    ws = GlobalWorkspace(max_chars=600)
    view = ws.build(state, belief_lines=["b" * 200] * 10, tools_text="\n".join(f"- tool_{i}" for i in range(40)))
    assert view.truncated is True
    assert view.char_count == len(view.text) <= 600
    assert view.text.endswith("…[truncated]")
    # sections keep their full content even when the rendered text is cut
    assert len(view.sections["high_impact_beliefs"]) == 10
    assert len(view.sections["available_tools"]) == 40
    # a larger budget with one oversized section truncates only that block
    big = GlobalWorkspace(max_chars=5000).build(MissionState(objective="short", notes=[]), belief_lines=["b" * 300] * 10)
    assert big.truncated is True and "- …[truncated]" in big.text and big.char_count < 5000
    assert GlobalWorkspace(max_chars=5000).build(MissionState(objective="short")).truncated is False


def test_workspace_never_lists_more_than_eight_ready_tasks():
    tasks = [Task(title=f"task {i}", priority=i / 20) for i in range(12)]
    state = MissionState(objective="many", tasks=tasks)
    view = GlobalWorkspace().build(state, ready_tasks=tasks)
    assert len(view.sections["ready_tasks"]) == 8
    assert [line.split(" ")[0] for line in view.sections["ready_tasks"]] == [t.id for t in tasks[:8]]
    assert view.text.count("hint=-: task ") == 8
    assert "ready_tasks" not in GlobalWorkspace().build(state, ready_tasks=[]).sections
