"""Tests for the SQLite-backed state store (cogos.persistence.store)."""

from __future__ import annotations

import sqlite3

import pytest

from cogos.persistence.migrations import apply_migrations
from cogos.persistence.store import StateStore, StoreConflict, content_hash
from cogos.schemas.beliefs import Claim, ClaimStatus, Evidence, EvidenceKind
from cogos.schemas.common import EpistemicStatus, Provenance, TrustLevel
from cogos.schemas.decisions import Decision
from cogos.schemas.events import Event
from cogos.schemas.memory import MemoryClass, MemoryRecord
from cogos.schemas.mission import MissionState, MissionStatus, SuccessCriterion, Task, TaskStatus, Unknown
from cogos.schemas.trace import TraceEvent
from cogos.schemas.world import CausalLink, Entity, Property, Relation, WorldModel


@pytest.fixture
def store(tmp_path):
    with StateStore(tmp_path / "db" / "cogos.db") as s:
        yield s


def _rich_state() -> MissionState:
    """A MissionState exercising nested collections: claims, evidence, tasks, world model."""
    ev = Evidence(
        summary="Primary source says X",
        kind=EvidenceKind.PRIMARY,
        provenance=Provenance(source="https://example.gov/report", trust=TrustLevel.UNTRUSTED_EXTERNAL, reliability=0.8, lineage=["example.gov"]),
        content_excerpt="X holds under condition Y",
        scope="2024 fiscal year",
        weight=0.9,
    )
    claim = Claim(
        proposition="X is true",
        status=ClaimStatus.SUPPORTED,
        epistemic_status=EpistemicStatus.INFERENCE,
        confidence=0.8,
        evidence_for=[ev.id],
        falsification_conditions=["a primary source showing not-X"],
        provenance=Provenance(source="specialist:researcher", trust=TrustLevel.SPECIALIST),
    )
    ev.supports_claim_ids.append(claim.id)
    t1 = Task(title="Inspect repository", operation_hint="inspect_files", parameters={"tool": "list_dir", "arguments": {"path": "."}}, priority=0.9, status=TaskStatus.DONE)
    t2 = Task(title="Implement feature", depends_on=[t1.id], parallel_safe=False, attempts=1, failure_signature="abc123", resolves_unknown_ids=[])
    acme = Entity(name="Acme", kind="organisation", properties=[Property(name="hq", value="Paris", confidence=0.9)], incentives=["growth"])
    widget = Entity(name="Widget", kind="artifact")
    world = WorldModel(
        entities=[acme, widget],
        relations=[Relation(source_id=acme.id, target_id=widget.id, kind="produces")],
        causal_links=[CausalLink(cause="price increase", effect="lower demand", mechanism="elasticity", strength=0.7)],
        external_dependencies=["supplier API"],
    )
    state = MissionState(
        objective="Determine whether X holds",
        status=MissionStatus.ACTIVE,
        success_criteria=[SuccessCriterion(description="X is established with evidence", verification_method="evidence")],
        explicit_constraints=["no network"],
        unknowns=[Unknown(question="Does X hold in 2025?", decision_importance=0.9)],
        tasks=[t1, t2],
        evidence=[ev],
        claims=[claim],
        world_model=world,
        confidence=0.4,
        progress=0.25,
        executive_model="claude-fable-5-1",
        notes=["seeded"],
    )
    state.usage.cycles = 3
    state.budget.max_cycles = 50
    return state


# --- missions -----------------------------------------------------------------------


def test_save_load_round_trip_preserves_all_fields(store):
    state = _rich_state()
    saved = store.save_mission(state)
    assert saved is state
    assert state.version == 1

    loaded = store.load_mission(state.mission_id)
    assert loaded is not None
    assert loaded.model_dump() == state.model_dump()
    # spot-check the nested aggregates survived the JSON round trip with types intact
    assert loaded.claims[0].evidence_for == [state.evidence[0].id]
    assert loaded.evidence[0].provenance.trust is TrustLevel.UNTRUSTED_EXTERNAL
    assert loaded.evidence[0].kind is EvidenceKind.PRIMARY
    assert loaded.tasks[1].depends_on == [state.tasks[0].id]
    assert loaded.tasks[0].status is TaskStatus.DONE
    assert loaded.world_model.entity_by_name("acme").properties[0].value == "Paris"
    assert loaded.world_model.relations[0].kind == "produces"
    assert loaded.world_model.causal_links[0].strength == 0.7
    assert loaded.usage.cycles == 3 and loaded.budget.max_cycles == 50
    assert loaded.status is MissionStatus.ACTIVE


def test_load_missing_mission_returns_none(store):
    assert store.load_mission("msn_does_not_exist") is None


def test_version_increments_and_stale_write_conflicts(store):
    state = _rich_state()
    store.save_mission(state)
    assert state.version == 1
    stale = store.load_mission(state.mission_id)
    assert stale.version == 1

    state.notes.append("second save")
    store.save_mission(state)
    assert state.version == 2
    assert store.load_mission(state.mission_id).version == 2

    stale.notes.append("conflicting write")
    with pytest.raises(StoreConflict):
        store.save_mission(stale)
    # the conflicting write must not have touched the stored row
    current = store.load_mission(state.mission_id)
    assert current.version == 2
    assert "conflicting write" not in current.notes


def test_mission_events_appended_per_save(store):
    state = _rich_state()
    store.save_mission(state)
    store.save_mission(state, event_kind="cycle_complete", payload={"cycle": 1})
    store.save_mission(state)

    events = store.mission_events(state.mission_id)
    assert [e["kind"] for e in events] == ["state_saved", "cycle_complete", "state_saved"]
    assert events[0]["payload"] == {"version": 1}
    assert events[1]["payload"] == {"cycle": 1}
    assert events[2]["payload"] == {"version": 3}
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3
    assert [e["seq"] for e in store.mission_events(state.mission_id, since_seq=seqs[0])] == seqs[1:]


def test_list_missions_filters_by_status(store):
    active = MissionState(objective="active one", status=MissionStatus.ACTIVE)
    done = MissionState(objective="done one", status=MissionStatus.COMPLETE)
    store.save_mission(active)
    store.save_mission(done)

    all_rows = store.list_missions()
    assert {r["mission_id"] for r in all_rows} == {active.mission_id, done.mission_id}
    assert set(all_rows[0].keys()) == {"mission_id", "status", "objective", "version", "created_at", "updated_at"}
    only_active = store.list_missions(status=MissionStatus.ACTIVE)
    assert [r["mission_id"] for r in only_active] == [active.mission_id]
    assert only_active[0]["status"] == "active"
    assert store.list_missions(status=MissionStatus.FAILED) == []
    assert [s.mission_id for s in store.iter_mission_states()] == [r["mission_id"] for r in all_rows]


def test_delete_mission_removes_state_events_and_traces(store):
    state = _rich_state()
    store.save_mission(state)
    store.record_trace(TraceEvent(mission_id=state.mission_id, kind="cycle_start", summary="c0"))
    store.delete_mission(state.mission_id)
    assert store.load_mission(state.mission_id) is None
    assert store.mission_events(state.mission_id) == []
    assert store.traces(state.mission_id) == []


# --- traces -----------------------------------------------------------------------------


def test_traces_record_and_query_by_kind(store):
    mid = "msn_trace_test"
    evs = [
        TraceEvent(mission_id=mid, cycle=0, kind="cycle_start", summary="start", ts="2026-01-01T00:00:00.000+00:00"),
        TraceEvent(mission_id=mid, cycle=0, kind="tool_call", summary="ls", data={"tool": "list_dir"}, cost={"ms": 3}, ts="2026-01-01T00:00:01.000+00:00"),
        TraceEvent(mission_id=mid, cycle=1, kind="tool_call", summary="cat", ts="2026-01-01T00:00:02.000+00:00"),
        TraceEvent(mission_id="msn_other", cycle=0, kind="tool_call", summary="other", ts="2026-01-01T00:00:03.000+00:00"),
    ]
    for ev in evs:
        store.record_trace(ev)

    by_mission = store.traces(mid)
    assert [t.summary for t in by_mission] == ["start", "ls", "cat"]
    assert by_mission[1].data == {"tool": "list_dir"} and by_mission[1].cost == {"ms": 3}
    assert by_mission[1] == evs[1]

    tool_calls = store.traces(mid, kind="tool_call")
    assert [t.summary for t in tool_calls] == ["ls", "cat"]
    assert len(store.traces(kind="tool_call")) == 3
    assert len(store.traces(mid, limit=2)) == 2
    assert store.traces(mid, kind="nope") == []

    # INSERT OR REPLACE: re-recording the same id updates instead of duplicating
    evs[0].summary = "start (updated)"
    store.record_trace(evs[0])
    assert [t.summary for t in store.traces(mid, kind="cycle_start")] == ["start (updated)"]


# --- decisions / calibration ------------------------------------------------------------


def test_decisions_and_calibration_samples(store):
    mid = "msn_dec"
    d1 = Decision(objective="pick", selected_option="A", concise_rationale="best", confidence=0.7, consequential=True, domain="research", timestamp="2026-01-01T00:00:00.000+00:00")
    d2 = Decision(objective="pick again", selected_option="B", concise_rationale="ok", outcome_success=True, timestamp="2026-01-01T00:00:01.000+00:00")
    store.record_decision(mid, d1)
    store.record_decision(mid, d2)
    store.record_decision("msn_other", Decision(objective="o", selected_option="C", concise_rationale="r"))

    got = store.decisions(mid)
    assert got == [d1, d2]
    assert len(store.decisions()) == 3

    d1.actual_outcome = "worked"
    d1.outcome_success = True
    store.record_decision(mid, d1)  # replace by decision_id
    assert len(store.decisions(mid)) == 2
    assert store.decisions(mid)[0].actual_outcome == "worked"

    store.record_calibration(mid, "research", 0.7, True, ref_id=d1.decision_id)
    store.record_calibration(mid, "research", 0.9, False)
    store.record_calibration(None, "code", 0.5, True)
    assert store.calibration_samples("research") == [(0.7, True), (0.9, False)]
    assert store.calibration_samples("code") == [(0.5, True)]
    assert len(store.calibration_samples()) == 3


# --- memories -----------------------------------------------------------------------------


def test_memory_put_get_hash_lookup_and_text_search(store):
    rec = MemoryRecord(memory_class=MemoryClass.SEMANTIC, content="SQLite   WAL mode improves concurrent reads", tags=["sqlite", "perf"], importance=0.8)
    store.put_memory(rec)
    assert rec.content_hash == content_hash(rec.content)
    assert rec.content_hash == content_hash("sqlite wal MODE improves concurrent reads")  # whitespace/case-normalised

    got = store.get_memory(rec.id)
    assert got is not None and got.content == rec.content and got.tags == ["sqlite", "perf"]
    assert store.get_memory("mem_missing") is None

    # hash lookup, optionally filtered by class
    assert store.find_memory_by_hash(rec.content_hash).id == rec.id
    assert store.find_memory_by_hash(rec.content_hash, MemoryClass.SEMANTIC).id == rec.id
    assert store.find_memory_by_hash(rec.content_hash, MemoryClass.FAILURE) is None
    assert store.find_memory_by_hash("nope") is None

    other = MemoryRecord(memory_class=MemoryClass.FAILURE, content="pip install failed behind the proxy", mission_id="msn_1", importance=0.3)
    store.put_memory(other)

    # full-text search (FTS5 available in this build) and LIKE fallback agree
    hits = store.search_memory_text("concurrent reads in sqlite")
    assert [m.id for m in hits] == [rec.id]
    assert store.search_memory_text("proxy")[0].id == other.id
    assert store.search_memory_text("") == []
    assert store.search_memory_text("a an")  == []  # all terms too short
    store._fts = False
    assert [m.id for m in store.search_memory_text("concurrent reads")] == [rec.id]
    store._fts = True

    # listing filters and superseded handling
    assert [m.id for m in store.all_memories()] == [rec.id, other.id]  # importance DESC
    assert [m.id for m in store.all_memories(memory_class=MemoryClass.FAILURE)] == [other.id]
    assert [m.id for m in store.all_memories(mission_id="msn_1")] == [other.id]
    other.superseded_by = "mem_newer"
    store.put_memory(other)
    assert store.all_memories(memory_class=MemoryClass.FAILURE) == []
    assert len(store.all_memories(memory_class=MemoryClass.FAILURE, include_superseded=True)) == 1
    assert store.find_memory_by_hash(other.content_hash) is None
    assert store.search_memory_text("proxy") == []

    store.delete_memory(rec.id)
    assert store.get_memory(rec.id) is None
    assert store.search_memory_text("sqlite") == []


# --- events / subscriptions / kv ------------------------------------------------------------


def test_events_pending_and_put(store):
    e1 = Event(kind="test_completed", source="ci", payload={"status": "green"}, occurred_at="2026-01-01T00:00:02.000+00:00")
    e2 = Event(kind="message", source="human", trusted=True, occurred_at="2026-01-01T00:00:01.000+00:00")
    e3 = Event(kind="webhook", handled=True, handled_at="2026-01-01T00:00:03.000+00:00")
    for e in (e1, e2, e3):
        store.put_event(e)

    pending = store.pending_events()
    assert [p.id for p in pending] == [e2.id, e1.id]  # ordered by occurred_at
    assert pending[1].payload == {"status": "green"} and pending[0].trusted is True

    e1.handled = True
    e1.handled_at = "2026-01-01T00:00:05.000+00:00"
    store.put_event(e1)  # replace
    assert [p.id for p in store.pending_events()] == [e2.id]


def test_subscriptions_with_wildcard(store):
    store.subscribe("msn_a", "test_completed", filter_={"repo": "x"}, affects={"task_ids": ["t1"]})
    store.subscribe("msn_b", "*")
    store.subscribe("msn_c", "file_changed")

    subs = store.subscriptions("test_completed")
    assert {(s["mission_id"], s["event_kind"]) for s in subs} == {("msn_a", "test_completed"), ("msn_b", "*")}
    a = next(s for s in subs if s["mission_id"] == "msn_a")
    assert a["filter"] == {"repo": "x"} and a["affects"] == {"task_ids": ["t1"]}
    assert len(store.subscriptions()) == 3
    assert {s["mission_id"] for s in store.subscriptions("deadline")} == {"msn_b"}


def test_kv_set_get_and_default(store):
    assert store.kv_get("missing") is None
    assert store.kv_get("missing", default=7) == 7
    store.kv_set("cfg", {"a": [1, 2], "b": None})
    assert store.kv_get("cfg") == {"a": [1, 2], "b": None}
    store.kv_set("cfg", "replaced")
    assert store.kv_get("cfg") == "replaced"


def test_skills_upsert_by_name(store):
    store.put_skill("skill_1", "triage", "candidate", {"steps": 1})
    store.put_skill("skill_2", "triage", "promoted", {"steps": 2})
    rows = store.skills()
    assert len(rows) == 1
    assert rows[0]["id"] == "skill_1" and rows[0]["status"] == "promoted" and rows[0]["data"] == {"steps": 2}
    assert store.skills(status="candidate") == []


# --- snapshots -------------------------------------------------------------------------------


def test_export_import_snapshot_round_trip(store, tmp_path):
    state = _rich_state()
    store.save_mission(state)
    store.save_mission(state)
    store.record_decision(state.mission_id, Decision(objective="o", selected_option="A", concise_rationale="r"))
    store.record_trace(TraceEvent(mission_id=state.mission_id, kind="cycle_start", summary="c0"))

    path = store.export_snapshot(state.mission_id, tmp_path / "snapshots")
    assert path == tmp_path / "snapshots" / f"{state.mission_id}.json"
    assert path.exists() and not path.with_suffix(".json.tmp").exists()

    with StateStore(tmp_path / "other.db") as other:
        imported = other.import_snapshot(path)
        assert imported.mission_id == state.mission_id
        assert imported.version == 1  # fresh insert in the target store
        loaded = other.load_mission(state.mission_id)
        assert loaded.model_dump(exclude={"version", "timestamps"}) == state.model_dump(exclude={"version", "timestamps"})
        assert loaded.timestamps.created_at == state.timestamps.created_at
        assert [d.selected_option for d in other.decisions(state.mission_id)] == ["A"]
        assert [t.summary for t in other.traces(state.mission_id)] == ["c0"]
        assert [e["kind"] for e in other.mission_events(state.mission_id)] == ["snapshot_imported"]
        assert other.mission_events(state.mission_id)[0]["payload"] == {"path": str(path)}

    with pytest.raises(StoreConflict):
        store.import_snapshot(path)  # already exists, overwrite=False

    # overwrite: replaces state, resets version, discards old history
    state.notes.append("changed after export")
    store.save_mission(state)
    assert state.version == 3
    restored = store.import_snapshot(path, overwrite=True)
    assert restored.version == 1
    reloaded = store.load_mission(state.mission_id)
    assert "changed after export" not in reloaded.notes
    assert [e["kind"] for e in store.mission_events(state.mission_id)] == ["snapshot_imported"]
    assert len(store.decisions(state.mission_id)) == 1


def test_import_snapshot_rejects_foreign_format(store, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"format": "something-else"}', encoding="utf-8")
    with pytest.raises(ValueError):
        store.import_snapshot(bad)
    with pytest.raises(KeyError):
        store.export_snapshot("msn_missing", tmp_path / "snap")


# --- health / migrations ----------------------------------------------------------------------


def test_health_reports_integrity_and_counts(store):
    state = _rich_state()
    store.save_mission(state)
    store.record_trace(TraceEvent(mission_id=state.mission_id, kind="cycle_start", summary="c0"))
    store.put_memory(MemoryRecord(memory_class=MemoryClass.META, content="hello"))
    h = store.health()
    assert h["integrity"] == "ok"
    assert h["schema_version"] == 1
    assert h["fts"] is True
    assert h["path"] == str(store.path)
    assert h["counts"] == {"missions": 1, "mission_events": 1, "traces": 1, "decisions": 0, "memories": 1, "events": 0, "skills": 0}


def test_apply_migrations_is_idempotent(tmp_path):
    db = tmp_path / "cogos.db"
    with StateStore(db) as s1:
        assert s1.schema_version == 1
        s1.kv_set("k", 1)
    with StateStore(db) as s2:
        assert s2.schema_version == 1
        assert s2.kv_get("k") == 1
        rows = s2._conn.execute("SELECT version FROM schema_version").fetchall()
        assert [r[0] for r in rows] == [1]

    conn = sqlite3.connect(str(db))
    try:
        assert apply_migrations(conn) == 1
        assert apply_migrations(conn) == 1
        assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1
    finally:
        conn.close()
