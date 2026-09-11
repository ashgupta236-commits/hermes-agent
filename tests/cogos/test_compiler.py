"""Tests for the mission compiler (cogos.mission.compiler)."""

from __future__ import annotations

import json

import pytest

from cogos.adapters.scripted import HeuristicExecutive, ScriptedExecutive
from cogos.config import DEFAULT_EXECUTIVE_MODEL, BudgetConfig, CogosConfig, ExecutiveConfig
from cogos.mission.compiler import MissionCompiler, gather_repo_context
from cogos.planner import Planner
from cogos.schemas.cognition import MissionCompilation
from cogos.schemas.common import EpistemicStatus, TrustLevel
from cogos.schemas.mission import Budget, MissionStatus, TaskStatus

REQUIREMENTS = "# Requirements\n\nAdd a `--verbose` flag to the CLI that prints timing information.\n"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"  # conftest seeds tmp_path itself with other entries
    root.mkdir()
    (root / "REQUIREMENTS.md").write_text(REQUIREMENTS, encoding="utf-8")
    (root / "tests").mkdir()
    (root / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (root / ".hidden").mkdir()
    (root / ".claude").mkdir()
    return root


def _config(tmp_path, **kw) -> CogosConfig:
    return CogosConfig(repo_root=tmp_path, home=tmp_path / ".cogos", **kw)


# --- repo context ---------------------------------------------------------------------------------


def test_gather_repo_context_detects_requirements_and_test_command(repo):
    ctx = gather_repo_context(repo)
    assert ctx["root"] == str(repo)
    assert ctx["has_requirements"] is True
    assert ctx["requirements_path"] == "REQUIREMENTS.md"
    assert ctx["requirements_text"] == REQUIREMENTS
    assert ctx["test_command"] == "python -m pytest -q"
    assert ctx["files"] == [".claude/", "REQUIREMENTS.md", "app.py", "tests/"]  # hidden dirs skipped except .claude/.cogos
    assert "readme_excerpt" not in ctx
    assert "git_status" in ctx and "git_log" in ctx  # not a repo here, but the keys are populated


def test_gather_repo_context_variants(tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Hello\n" + "x" * 3000, encoding="utf-8")
    (tmp_path / "SPEC.md").write_text("spec " * 2000, encoding="utf-8")
    for i in range(70):
        (tmp_path / f"f{i:02d}.txt").write_text("", encoding="utf-8")
    ctx = gather_repo_context(tmp_path)
    assert ctx["test_command"] == "npm test"
    assert ctx["requirements_path"] == "SPEC.md" and len(ctx["requirements_text"]) == 6000
    assert len(ctx["readme_excerpt"]) == 1500
    assert len(ctx["files"]) == 60
    bare = gather_repo_context(tmp_path / "nowhere")
    assert bare["files"] == [] and "test_command" not in bare and "has_requirements" not in bare


# --- compile -----------------------------------------------------------------------------------------


def test_compile_implementation_objective_with_heuristic_executive(repo):
    cfg = _config(repo)
    compiler = MissionCompiler(HeuristicExecutive(), cfg)
    ctx = gather_repo_context(repo)
    state, comp, meta = compiler.compile("Implement a --verbose flag for the CLI", context=ctx, human_context="ship today")

    assert meta["used_fallback"] is False
    assert meta["response"]["ok"] is True and meta["compilation"]["mission_kind"] == "implementation"
    assert isinstance(comp, MissionCompilation) and comp.mission_kind == "implementation"
    assert comp.required_tests == ["python -m pytest -q"]

    # the executive received a compile request carrying the context and requirements
    req = compiler.adapter.calls[0]
    assert req.kind == "compile" and req.schema_name == "MissionCompilation"
    assert req.model == DEFAULT_EXECUTIVE_MODEL and req.effort == cfg.executive.effort
    assert req.metadata["context"]["has_requirements"] is True
    assert "REQUIREMENTS DOCUMENT" in req.prompt and REQUIREMENTS.strip() in req.prompt
    assert "ADDITIONAL HUMAN CONTEXT: ship today" in req.prompt
    assert req.output_schema["additionalProperties"] is False

    # materialised state
    assert state.status is MissionStatus.DRAFT
    assert state.executive_model == DEFAULT_EXECUTIVE_MODEL == cfg.executive.model
    assert state.budget == Budget(**cfg.budget.model_dump())
    assert state.resources["mission_kind"] == "implementation" and state.resources["repo_root"] == str(repo)
    assert state.resources["required_tests"] == ["python -m pytest -q"] and state.resources["controller"] == {}
    assert state.notes == ["human context: ship today"]
    assert [c.description for c in state.success_criteria] == [c.description for c in comp.success_criteria]
    assert [c.explicit for c in state.success_criteria] == [True, False]
    assert state.inferred_constraints == comp.inferred_constraints
    assert [a.statement for a in state.assumptions] == comp.assumptions and state.assumptions[0].load_bearing is True
    assert [u.question for u in state.unknowns] == [u.question for u in comp.unknowns]
    assert state.unknowns[0].decision_importance == 0.9
    assert [r.description for r in state.risks] == [r.description for r in comp.risks]
    assert state.confidence == comp.confidence_in_interpretation * 0.5 and state.progress == 0.0
    assert state.timestamps.started_at is None

    # goals and tasks with resolved dependencies, no cycles
    assert [g.title for g in state.goals] == [g.title for g in comp.goals]
    assert len(state.tasks) == len(comp.tasks) == 6
    task_ids = {t.id for t in state.tasks}
    goal_ids = {g.id for g in state.goals}
    by_title = {t.title: t for t in state.tasks}
    for t in state.tasks:
        assert all(d in task_ids for d in t.depends_on), t.title
        assert t.goal_id in goal_ids, t.title
    assert Planner(state).dag_valid() == (True, "ok")
    assert by_title["Implement the feature"].depends_on == [by_title["Choose architecture and implementation plan"].id]
    assert sorted(by_title["Choose architecture and implementation plan"].depends_on) == sorted([by_title["Inspect repository layout"].id, by_title["Read the requirements"].id])
    assert by_title["Implement the feature"].parallel_safe is False
    assert by_title["Read the requirements"].parameters["arguments"]["path"] == "REQUIREMENTS.md"
    assert by_title["Read the requirements"].priority == 0.95
    assert by_title["Run the test suite"].parameters["commands"] == ["python -m pytest -q"]

    # readiness and cross-links computed during materialisation
    ready = {t.title for t in state.tasks if t.status is TaskStatus.READY}
    assert ready == {"Inspect repository layout", "Read the requirements"}
    assert by_title["Inspect repository layout"].resolves_unknown_ids == [state.unknowns[0].id]
    assert by_title["Implement the feature"].addresses_criterion_ids == [state.success_criteria[0].id]
    assert set(by_title["Run the test suite"].addresses_criterion_ids) == {c.id for c in state.success_criteria}


def test_compile_uses_repo_context_by_default_and_copies_budget(repo):
    cfg = _config(repo, executive=ExecutiveConfig(model="claude-fable-5-1-custom", adapter="scripted"), budget=BudgetConfig(max_cycles=7, max_model_calls=9, max_cost_usd=1.5))
    compiler = MissionCompiler(HeuristicExecutive(), cfg)
    state, comp, meta = compiler.compile("Implement the thing", permissions={"network": False})
    assert compiler.adapter.calls[0].metadata["context"]["has_requirements"] is True  # gathered from cfg.repo_root
    assert state.executive_model == "claude-fable-5-1-custom"
    assert state.budget == Budget(max_cycles=7, max_model_calls=9, max_subagents=20, max_cost_usd=1.5, max_wall_clock_seconds=None)
    assert state.permissions == {"network": False}

    explicit = Budget(max_cycles=3)
    state2, _, _ = compiler.compile("Implement the thing", context={}, budget=explicit)
    assert state2.budget is explicit
    # without requirements in context the reqs task is de-prioritised and the fallback path stays sane
    assert next(t for t in state2.tasks if t.title == "Read the requirements").priority == 0.6


def test_compile_falls_back_when_compilation_invalid_or_empty(repo):
    cfg = _config(repo)
    ctx = gather_repo_context(repo)

    # valid schema but no tasks -> defaults
    adapter = ScriptedExecutive(responses={"compile": [{"interpretation": "x"}]})
    state, comp, meta = MissionCompiler(adapter, cfg).compile("Implement a thing", context=ctx)
    assert meta["used_fallback"] is True
    assert comp.mission_kind == "implementation" and len(comp.tasks) == 6
    assert meta["response"]["parsed"] == {"interpretation": "x"}
    assert len(state.tasks) == 6 and Planner(state).dag_valid() == (True, "ok")

    # malformed structured output -> validation error -> defaults
    adapter = ScriptedExecutive(responses={"compile": [{"interpretation": 5, "tasks": "nope"}]})
    state, comp, meta = MissionCompiler(adapter, cfg).compile("Fix the broken login flow", context=ctx)
    assert meta["used_fallback"] is True and comp.mission_kind == "repair"
    assert [h.statement for h in state.hypotheses] == [h.statement for h in comp.hypotheses]
    assert state.hypotheses[0].prior == 0.4 == state.hypotheses[0].confidence

    # adapter failure -> defaults, response recorded
    adapter = ScriptedExecutive(fail_kinds={"compile": "transient"})
    state, comp, meta = MissionCompiler(adapter, cfg).compile("Research widgets", context=ctx)
    assert meta["used_fallback"] is True and meta["response"]["ok"] is False and meta["response"]["error_kind"] == "transient"
    assert comp.mission_kind == "research" and state.tasks


def test_compile_tolerates_unknown_dependency_keys_and_clamps_values(repo):
    cfg = _config(repo)
    compilation = {
        "interpretation": "custom plan",
        "mission_kind": "general",
        "success_criteria": [{"description": "Widget works", "verification_method": "manual"}],
        "known_facts": ["repo uses pytest"],
        "unknowns": [{"question": "How big?", "decision_importance": 7, "estimated_cost": 0}],
        "hypotheses": [{"question": "q", "statement": "s", "prior": -1}],
        "risks": [{"description": "r", "probability": 2, "impact": 0.5}],
        "human_requests": [{"kind": "decision", "question": "colour?", "why_not_inferable": "taste", "options": ["red", "blue"]}],
        "goals": [{"key": "g", "title": "Goal"}],
        "tasks": [
            {"key": "a", "title": "A", "goal_key": "g", "depends_on": ["ghost"]},
            {"key": "b", "title": "B", "goal_key": "g", "depends_on": ["a", "ghost2", "b"], "addresses_criteria": ["widget works"]},
            {"key": "c", "title": "C", "goal_key": "missing-goal", "depends_on": ["b"], "parameters_json": "not json"},
        ],
    }
    adapter = ScriptedExecutive(responses={"compile": [compilation]})
    state, comp, meta = MissionCompiler(adapter, cfg).compile("Do the custom thing", context={})
    assert meta["used_fallback"] is False
    assert comp.interpretation == "custom plan" and [t.key for t in comp.tasks] == ["a", "b", "c"]
    a, b, c = state.tasks
    assert a.depends_on == []
    assert b.depends_on == [a.id]
    assert c.depends_on == [b.id] and c.goal_id is None and c.parameters == {"_resolves_unknowns": [], "_addresses_criteria": []}
    assert a.status is TaskStatus.READY and b.status is TaskStatus.PENDING
    assert b.addresses_criterion_ids == [state.success_criteria[0].id]
    assert Planner(state).dag_valid() == (True, "ok")

    # out-of-range numbers are clamped rather than rejected
    assert state.unknowns[0].decision_importance == 1.0 and state.unknowns[0].estimated_cost == 0.05
    assert state.hypotheses[0].prior == 0.0 and state.risks[0].probability == 1.0
    assert state.known_facts[0].provenance.trust is TrustLevel.HUMAN_PRINCIPAL and state.known_facts[0].epistemic_status is EpistemicStatus.OBSERVATION
    assert state.human_requests[0].options == ["red", "blue"] and state.human_requests[0].independent_work_remaining is True
    assert json.loads(json.dumps(meta["compilation"]))["tasks"][2]["parameters_json"] == "not json"


def test_materialise_breaks_cycles_from_model_output(repo):
    compilation = {
        "interpretation": "cyclic",
        "tasks": [
            {"key": "a", "title": "A", "depends_on": ["c"]},
            {"key": "b", "title": "B", "depends_on": ["a"]},
            {"key": "c", "title": "C", "depends_on": ["b"]},
        ],
    }
    adapter = ScriptedExecutive(responses={"compile": [compilation]})
    state, _, meta = MissionCompiler(adapter, _config(repo)).compile("Cycle", context={})
    assert meta["used_fallback"] is False
    assert Planner(state).dag_valid() == (True, "ok")
    assert sum(len(t.depends_on) for t in state.tasks) == 2
    # readiness is recomputed after the cycle is broken, so the freed task is READY;
    # the next planner pass (as the executive loop does every cycle) surfaces the freed task
    assert any(t.status is TaskStatus.READY for t in state.tasks)
    assert len(Planner(state).compute_ready()) == 1


def test_shipped_config_example_matches_the_real_schema_and_defaults() -> None:
    """`cogos.yaml.example` must stay loadable and must not drift from CogosConfig defaults."""
    import pathlib

    import yaml

    from cogos.config import DEFAULT_EXECUTIVE_MODEL, CogosConfig

    root = pathlib.Path(__file__).resolve().parents[2]
    raw = yaml.safe_load((root / "cogos.yaml.example").read_text(encoding="utf-8"))
    raw["repo_root"] = str(root)
    cfg = CogosConfig.model_validate(raw)  # unknown keys or wrong types fail here
    defaults = CogosConfig(repo_root=root)

    assert cfg.executive.model == DEFAULT_EXECUTIVE_MODEL
    assert cfg.executive.allow_cheaper_specialist_models is False
    assert cfg.governance.always_require_human == defaults.governance.always_require_human
    assert cfg.budget.model_dump() == defaults.budget.model_dump()
    assert cfg.memory.model_dump() == defaults.memory.model_dump()
    assert cfg.workspace_max_chars == defaults.workspace_max_chars
