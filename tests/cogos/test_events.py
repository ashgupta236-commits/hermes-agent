"""Tests for cogos.events: bus routing, trust, acks, wake targets, sources."""

from __future__ import annotations

import json
import time

import pytest

from cogos.events import (
    DeadlineSource,
    EventBus,
    ExternalJobSource,
    FileWatchSource,
    ScheduledSource,
    TestCompletionSource,
    filter_matches,
)
from cogos.persistence.store import StateStore
from cogos.schemas.events import Event
from cogos.schemas.tools import ToolResult


@pytest.fixture
def store(tmp_path):
    with StateStore(tmp_path / "db.sqlite") as s:
        yield s


def test_filter_matches_subset_semantics():
    assert filter_matches({}, {"a": 1})
    assert filter_matches({"a": 1}, {"a": 1, "b": 2})
    assert not filter_matches({"a": 2}, {"a": 1})
    assert not filter_matches({"c": 1}, {"a": 1})
    assert filter_matches({"meta": {"x": 1}}, {"meta": {"x": 1, "y": 2}})
    assert filter_matches({"tags": ["a"]}, {"tags": ["a", "b"]})


def test_routing_explicit_and_subscription_filters(store):
    bus = EventBus(store)
    bus.subscribe("msn_sub", "file_changed", filter_={"change": "modified"}, affects={"claims": ["clm_1"], "unknowns": ["unk_1"]})
    bus.subscribe("msn_star", "*")
    bus.subscribe("msn_other", "file_changed", filter_={"change": "deleted"})

    ev = bus.emit(Event(kind="file_changed", source="file_watch", payload={"path": "a.py", "change": "modified"}, mission_ids=["msn_explicit"]))
    assert set(ev.routed_to) == {"msn_explicit", "msn_sub", "msn_star"}
    assert "msn_other" not in ev.routed_to
    assert [e.id for e in bus.pending_for("msn_sub")] == [ev.id]
    assert [e.id for e in bus.pending_for("msn_explicit")] == [ev.id]
    assert bus.pending_for("msn_other") == []
    affected = bus.affected_state(ev, "msn_sub")
    assert affected["claims"] == ["clm_1"] and affected["unknowns"] == ["unk_1"] and affected["tasks"] == []
    assert bus.affected_state(ev, "msn_explicit") == {"claims": [], "unknowns": [], "tasks": []}


def test_untrusted_enforced_for_external_sources(store):
    bus = EventBus(store)
    ev = bus.emit(Event(kind="webhook", source="github", payload={"x": 1}, mission_ids=["m"], trusted=True))
    assert ev.trusted is False
    assert store.pending_events()[0].trusted is False
    human = bus.emit(Event(kind="human_input", source="human", payload={}, mission_ids=["m"], trusted=True))
    assert human.trusted is True


def test_acks_and_wake_targets(store):
    bus = EventBus(store)
    e1 = bus.emit(Event(kind="message", source="system", payload={}, mission_ids=["msn_b", "msn_a"]))
    e2 = bus.emit(Event(kind="message", source="system", payload={}, mission_ids=["msn_c", "msn_a"]))
    assert bus.wake_targets() == ["msn_a", "msn_b", "msn_c"]

    bus.mark_handled(e1.id, "msn_a")
    assert bus.pending_for("msn_a") and bus.pending_for("msn_a")[0].id == e2.id
    assert bus.pending_for("msn_b")[0].id == e1.id
    assert bus.wake_targets() == ["msn_a", "msn_b", "msn_c"]
    assert store.pending_events()[0].handled is False  # msn_b has not acked yet

    updated = bus.mark_handled(e1.id, "msn_b")
    assert updated is not None and updated.handled is True and updated.handled_at
    assert [e.id for e in store.pending_events()] == [e2.id]
    assert bus.wake_targets() == ["msn_a", "msn_c"]
    bus.mark_handled(e2.id, "msn_a")
    bus.mark_handled(e2.id, "msn_c")
    assert store.pending_events() == [] and bus.wake_targets() == []


def test_unrouted_event_is_persisted_as_handled(store):
    bus = EventBus(store)
    ev = bus.emit(Event(kind="custom", source="system", payload={}))
    assert ev.routed_to == [] and ev.handled is True
    assert store.pending_events() == []


# -- sources --------------------------------------------------------------------------


def test_file_watch_detects_create_modify_delete(tmp_path):
    watched = tmp_path / "watched"
    watched.mkdir()
    f = watched / "a.txt"
    f.write_text("one", encoding="utf-8")
    state = tmp_path / "watch_state.json"
    src = FileWatchSource([watched], state_path=state, mission_ids=["msn_w"])
    assert src.poll() == []  # baseline snapshot, nothing changed yet
    assert state.exists() and str(f) in json.loads(state.read_text(encoding="utf-8"))

    f.write_text("two", encoding="utf-8")
    g = watched / "b.txt"
    g.write_text("new", encoding="utf-8")
    events = src.poll()
    by_path = {e.payload["path"]: e for e in events}
    assert by_path[str(f)].payload["change"] == "modified"
    assert by_path[str(g)].payload["change"] == "created"
    assert all(e.kind == "file_changed" and e.trusted is False and e.mission_ids == ["msn_w"] for e in events)
    assert src.poll() == []

    g.unlink()
    events = src.poll()
    assert len(events) == 1 and events[0].payload["change"] == "deleted"

    # A fresh source resumes from the persisted snapshot: no spurious events.
    resumed = FileWatchSource([watched], state_path=state)
    assert resumed.poll() == []


def test_deadline_source_fires_once(tmp_path):
    src = DeadlineSource([("msn_d", "2026-01-01T00:00:00+00:00"), ("msn_late", "2030-01-01T00:00:00+00:00")])
    events = src.poll(now="2026-06-01T00:00:00+00:00")
    assert len(events) == 1 and events[0].kind == "deadline" and events[0].mission_ids == ["msn_d"]
    assert events[0].trusted is True
    assert src.poll(now="2026-06-02T00:00:00+00:00") == []


def test_scheduled_source_respects_interval():
    src = ScheduledSource(60, last_fire_at="2026-01-01T00:00:00+00:00", mission_ids=["msn_s"])
    assert src.poll(now="2026-01-01T00:00:30+00:00") == []
    events = src.poll(now="2026-01-01T00:01:00+00:00")
    assert len(events) == 1 and events[0].kind == "scheduled" and events[0].mission_ids == ["msn_s"]
    assert src.poll(now="2026-01-01T00:01:30+00:00") == []
    fresh = ScheduledSource(60, None)
    assert len(fresh.poll()) == 1


def test_external_job_and_test_completion_sources():
    jobs = ExternalJobSource()
    ev = jobs.complete("job-1", "msn_j", {"status": "ok", "artifact": "out.zip"})
    assert ev.kind == "job_completed" and ev.mission_ids == ["msn_j"] and ev.trusted is False
    assert ev.payload["job_id"] == "job-1" and ev.payload["result"]["artifact"] == "out.zip"
    assert jobs.poll() == [ev] and jobs.poll() == []

    tr = ToolResult(call_id="c", tool="run_tests", ok=False, output="3 passed, 1 failed", data={"passed": 3, "failed": 1})
    tev = TestCompletionSource.from_tool_result("msn_t", tr)
    assert tev.kind == "test_completed" and tev.mission_ids == ["msn_t"]
    assert tev.payload["ok"] is False and tev.payload["counts"] == {"passed": 3, "failed": 1}
    assert tev.payload["summary"] == "3 passed, 1 failed"
    assert time.time() > 0  # sources never spawn threads; nothing to join
