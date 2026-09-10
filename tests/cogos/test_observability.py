"""Tests for cogos.observability: tracer, ledger, journal, calibration."""

from __future__ import annotations

import json

import pytest

from cogos.adapters.base import CognitionResponse
from cogos.observability import CalibrationTracker, DecisionJournal, ResourceLedger, Tracer
from cogos.persistence.store import StateStore
from cogos.schemas.decisions import Decision
from cogos.schemas.mission import Budget, MissionState, ResourceUsage
from cogos.schemas.tools import ToolResult


@pytest.fixture
def store(tmp_path):
    with StateStore(tmp_path / "db.sqlite") as s:
        yield s


# -- tracer -------------------------------------------------------------------------


def test_tracer_persists_and_mirrors_to_jsonl(store, tmp_path, capsys):
    jsonl = tmp_path / "trace.jsonl"
    tracer = Tracer(store, mission_id="msn_1", stdout=True, jsonl_path=jsonl)
    tracer.set_cycle(3)
    ev = tracer.emit("tool_call", "ran pytest", data={"tool": "run_tests"}, cost={"duration_ms": 12})
    assert ev.cycle == 3 and ev.mission_id == "msn_1"
    stored = store.traces("msn_1")
    assert [t.id for t in stored] == [ev.id]
    assert stored[0].data == {"tool": "run_tests", "seq": 1}
    lines = jsonl.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["id"] == ev.id
    out = capsys.readouterr().out
    assert "tool_call" in out and "ran pytest" in out and "duration_ms=12" in out


def test_tracer_explain_answers_observability_questions(store):
    tracer = Tracer(store, mission_id="msn_x")
    tracer.emit("mission_compiled", "compiled", data={"objective": "Fix the flaky login test"})
    tracer.set_cycle(1)
    tracer.emit("select", "chose inspect_files", data={"operation": "inspect_files", "rationale": "need to see the test"})
    tracer.emit("tool_call", "read file", data={"tool": "read_file"}, cost={"input_tokens": 100, "cost_usd": 0.01})
    tracer.emit("decision", "retry strategy", data={"selected_option": "add wait", "confidence": 0.7})
    tracer.emit("specialist", "spawned debugger", data={"role": "debugger"}, cost={"cost_usd": 0.02})
    tracer.emit("failure", "test still failing", data={"reason": "timeout"})
    tracer.emit("retry", "retrying with longer wait", data={"attempt": 2})
    tracer.emit("verify", "tests pass", data={"status": "passed"})
    tracer.emit("assess", "assessment", data={"unresolved_uncertainties": ["is the fix stable under load?"]})
    ex = tracer.explain("msn_x")
    assert ex["objective"] == "Fix the flaky login test"
    assert ex["operations"][0]["operation"] == "inspect_files"
    assert ex["operations"][0]["rationale"] == "need to see the test"
    assert ex["tool_calls"][0]["tool"] == "read_file"
    assert ex["decisions"][0]["selected_option"] == "add wait"
    assert ex["specialists"][0]["role"] == "debugger"
    assert len(ex["failures"]) == 1 and len(ex["retries"]) == 1
    assert ex["verifications"][0]["status"] == "passed"
    assert ex["total_cost"]["cost_usd"] == pytest.approx(0.03)
    assert ex["total_cost"]["input_tokens"] == 100
    assert ex["unresolved_uncertainties"] == ["is the fix stable under load?"]
    lines = tracer.timeline("msn_x")
    assert len(lines) == 9 and any("mission_compiled" in line for line in lines)


def test_tracer_span_captures_error(store):
    tracer = Tracer(store, mission_id="msn_span")
    with pytest.raises(ValueError):
        with tracer.span("operation", "risky step"):
            raise ValueError("boom")
    events = sorted(store.traces("msn_span", kind="operation"), key=lambda t: t.data["seq"])
    assert len(events) == 2
    start, end = events
    assert start.data["phase"] == "start"
    assert end.data["phase"] == "end" and end.data["ok"] is False
    assert end.data["error"] == "boom" and end.data["error_type"] == "ValueError"
    assert end.parent_id == start.id and "duration_ms" in end.data

    with tracer.span("operation", "good step"):
        pass
    ok_end = max(store.traces("msn_span", kind="operation"), key=lambda t: t.data["seq"])
    assert ok_end.data["ok"] is True


# -- ledger ----------------------------------------------------------------------------


def test_ledger_accounting_and_budget_breach():
    usage = ResourceUsage()
    ledger = ResourceLedger(usage)
    ledger.add_model_call(CognitionResponse(ok=True, model_requested="m", input_tokens=10, output_tokens=5, cost_usd=0.5))
    ledger.add_tool_call(ToolResult(call_id="c1", tool="web_fetch", ok=True))
    ledger.add_tool_call(ToolResult(call_id="c2", tool="read_file", ok=True))
    ledger.add_tool_call(ToolResult(call_id="c3", tool="custom", ok=True, data={"network": True}))
    ledger.add_retry()
    ledger.add_subagent(2)
    ledger.add_cycle()
    ledger.add_wall_clock(4.5)
    snap = ledger.snapshot()
    assert snap["model_calls"] == 1 and snap["input_tokens"] == 10 and snap["output_tokens"] == 5
    assert snap["tool_calls"] == 3 and snap["network_requests"] == 2
    assert snap["retries"] == 1 and snap["subagents_spawned"] == 2 and snap["cycles"] == 1
    assert snap["total_tokens"] == 15
    assert usage.wall_clock_seconds == pytest.approx(4.5)

    assert ledger.over_budget(Budget()) is None
    reason = ledger.over_budget(Budget(max_cost_usd=0.25))
    assert reason is not None and "cost budget" in reason and "$0.50" in reason
    reason = ledger.over_budget(Budget(max_cycles=1))
    assert reason is not None and "cycle budget" in reason and "1/1" in reason
    reason = ledger.over_budget(Budget(max_subagents=2))
    assert reason is not None and "subagent" in reason


# -- journal -----------------------------------------------------------------------------


def test_journal_record_resolve_and_calibration_sample(store):
    state = MissionState(objective="ship it")
    journal = DecisionJournal(store, state)
    d = Decision(
        objective="pick a db",
        available_options=["sqlite", "postgres"],
        selected_option="sqlite",
        concise_rationale="simplest",
        confidence=0.8,
        review_trigger="if write volume exceeds 1k/s",
        consequential=True,
        domain="architecture",
    )
    journal.record(d)
    assert state.decisions == [d]
    assert [x.decision_id for x in store.decisions(state.mission_id)] == [d.decision_id]
    assert journal.pending_reviews() == [d]
    assert journal.consequential() == [d]

    resolved = journal.resolve(d.decision_id, "worked fine", True)
    assert resolved is d and d.actual_outcome == "worked fine" and d.outcome_success is True
    assert store.decisions(state.mission_id)[0].outcome_success is True
    assert store.calibration_samples("architecture") == [(0.8, True)]
    assert journal.pending_reviews() == []
    assert journal.resolve("missing", "x", False) is None


# -- calibration ---------------------------------------------------------------------------


def test_calibration_insufficient_data(store):
    tracker = CalibrationTracker(store)
    for _ in range(4):
        store.record_calibration(None, "general", 0.9, True)
    rep = tracker.report("general")
    assert rep.n == 4 and "insufficient data for calibration" in rep.note
    assert rep.overconfident is False
    assert tracker.adjusted_confidence("general", 0.9) == 0.9
    recs = tracker.recommendations("general")
    assert all(recs[k] == 1.0 for k in ("verification_depth", "specialist_spawn_threshold", "investigation_depth", "escalation_threshold"))


def test_calibration_shrinks_overconfident_predictions(store):
    tracker = CalibrationTracker(store)
    # 12 predictions at 0.9, only half of them right -> overconfident.
    for i in range(12):
        store.record_calibration(None, "research", 0.9, i % 2 == 0)
    rep = tracker.report("research", bins=5)
    assert rep.n == 12 and rep.overconfident is True
    assert rep.expected_calibration_error == pytest.approx(0.4, abs=1e-3)
    assert rep.brier_score > 0
    top = rep.bins[-1]
    assert top.n == 12 and top.observed_rate == pytest.approx(0.5) and top.mean_predicted == pytest.approx(0.9)
    adjusted = tracker.adjusted_confidence("research", 0.9)
    assert adjusted == pytest.approx(0.7)  # halfway between 0.9 and observed 0.5
    recs = tracker.recommendations("research")
    assert recs["verification_depth"] > 1.0 and recs["investigation_depth"] > 1.0
    assert recs["specialist_spawn_threshold"] < 1.0 and recs["escalation_threshold"] < 1.0
    # Other domains are unaffected.
    assert tracker.adjusted_confidence("other", 0.9) == 0.9
