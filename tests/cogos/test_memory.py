"""Tests for cogos.memory.MemoryManager."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cogos.config import MemoryConfig
from cogos.memory import MemoryManager
from cogos.persistence.store import StateStore, content_hash
from cogos.schemas.memory import MemoryClass, MemoryRecord
from cogos.schemas.mission import MissionState, MissionStatus


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "db.sqlite")
    yield s
    s.close()


@pytest.fixture
def mm(store):
    return MemoryManager(store, MemoryConfig(min_importance_to_store=0.35))


# -- selective write -----------------------------------------------------------


def test_selective_write_threshold(mm):
    assert mm.remember(MemoryClass.SEMANTIC, "trivial detail", importance=0.1) is None
    kept = mm.remember(MemoryClass.SEMANTIC, "important detail", importance=0.5)
    assert kept is not None
    assert kept.content_hash == content_hash("important detail")
    assert mm.store.get_memory(kept.id) is not None


def test_failure_and_procedural_bypass_threshold(mm):
    fail = mm.remember(MemoryClass.FAILURE, "pip install without a venv broke the system python", importance=0.05)
    proc = mm.remember(MemoryClass.PROCEDURAL, "run pytest -q before committing", importance=0.0)
    assert fail is not None and proc is not None
    assert mm.stats()["failure"] == 1
    assert mm.stats()["procedural"] == 1


def test_empty_content_rejected(mm):
    assert mm.remember(MemoryClass.SEMANTIC, "   ", importance=0.9) is None


def test_default_ttl_applied(store):
    mm = MemoryManager(store, MemoryConfig(default_ttl_days=7))
    rec = mm.remember(MemoryClass.EPISODIC, "something that should decay", importance=0.6)
    assert rec is not None and rec.expires_at is not None
    created = datetime.fromisoformat(rec.created_at)
    expires = datetime.fromisoformat(rec.expires_at)
    assert expires - created == timedelta(days=7)


# -- dedup -----------------------------------------------------------------------


def test_dedup_merges_into_existing(mm):
    a = mm.remember(MemoryClass.SEMANTIC, "The API rate limit is 100 requests per minute", tags=["api"], confidence=0.5)
    b = mm.remember(MemoryClass.SEMANTIC, "the api  rate limit is 100 requests per minute", tags=["limits"], confidence=0.9)
    assert a is not None and b is not None
    assert b.id == a.id
    assert b.version == 2
    assert b.confidence == 0.9
    assert b.tags == ["api", "limits"]
    assert mm.stats()["semantic"] == 1
    # confidence never decreases on merge
    c = mm.remember(MemoryClass.SEMANTIC, "The API rate limit is 100 requests per minute", confidence=0.2)
    assert c.id == a.id and c.confidence == 0.9 and c.version == 3


def test_dedup_is_per_class(mm):
    a = mm.remember(MemoryClass.SEMANTIC, "deploys happen on fridays", importance=0.5)
    b = mm.remember(MemoryClass.EPISODIC, "deploys happen on fridays", importance=0.5)
    assert a.id != b.id
    assert mm.stats()["semantic"] == 1 and mm.stats()["episodic"] == 1


# -- contradictions --------------------------------------------------------------


def test_numeric_contradiction_detected_and_both_kept(mm):
    a = mm.remember(MemoryClass.SEMANTIC, "VAT rate is 15%", tags=["tax"], importance=0.6)
    b = mm.remember(MemoryClass.SEMANTIC, "VAT rate is 5%", tags=["tax"], importance=0.6)
    assert a is not None and b is not None
    a2 = mm.store.get_memory(a.id)
    assert b.id in a2.contradicts
    assert a.id in b.contradicts
    assert a2.superseded_by is None and b.superseded_by is None
    pairs = mm.contradictions()
    assert len(pairs) == 1
    assert {pairs[0][0].id, pairs[0][1].id} == {a.id, b.id}
    # both remain retrievable
    got = {r.id for r in mm.retrieve("VAT rate")}
    assert {a.id, b.id} <= got


def test_negation_contradiction_detected(mm):
    a = mm.remember(MemoryClass.SEMANTIC, "The legacy endpoint is deprecated", importance=0.6)
    b = mm.remember(MemoryClass.SEMANTIC, "The legacy endpoint is not deprecated", importance=0.6)
    assert b.id in mm.store.get_memory(a.id).contradicts
    assert a.id in b.contradicts
    assert len(mm.contradictions()) == 1


def test_no_longer_contradiction(mm):
    a = mm.remember(MemoryClass.RELATIONAL, "Alice is the project lead", tags=["team"], importance=0.6)
    b = mm.remember(MemoryClass.RELATIONAL, "Alice is no longer the project lead", tags=["team"], importance=0.6)
    assert a.id in b.contradicts


def test_no_false_contradiction_for_compatible_statements(mm):
    mm.remember(MemoryClass.SEMANTIC, "VAT rate is 15%", tags=["tax"], importance=0.6)
    mm.remember(MemoryClass.SEMANTIC, "VAT rate is 15% since 2020", tags=["tax"], importance=0.6)
    mm.remember(MemoryClass.SEMANTIC, "Income tax filing deadline is April 15", tags=["tax"], importance=0.6)
    assert mm.contradictions() == []


def test_explicit_contradicts_link(mm):
    a = mm.remember(MemoryClass.SEMANTIC, "the cache is warmed at boot", importance=0.6)
    rec = MemoryRecord(memory_class=MemoryClass.SEMANTIC, content="cold start takes eight seconds", importance=0.6, contradicts=[a.id])
    b = mm.write(rec)
    assert b.id in mm.store.get_memory(a.id).contradicts
    assert len(mm.contradictions()) == 1


def test_contradiction_resolved_by_supersede(mm):
    a = mm.remember(MemoryClass.SEMANTIC, "VAT rate is 15%", importance=0.6)
    b = mm.remember(MemoryClass.SEMANTIC, "VAT rate is 5%", importance=0.6)
    assert len(mm.contradictions()) == 1
    c = mm.remember(MemoryClass.SEMANTIC, "VAT rate is 5% (confirmed by finance)", importance=0.8, data={"supersedes": a.id})
    assert c is not None
    assert mm.store.get_memory(a.id).superseded_by == c.id
    assert mm.contradictions() == []
    # b keeps its history: the link to the superseded record is retained, not scrubbed
    assert a.id in mm.store.get_memory(b.id).contradicts


# -- versioning ------------------------------------------------------------------


def test_supersede_marks_old_record(mm):
    old = mm.remember(MemoryClass.PROCEDURAL, "deploy with make deploy", importance=0.5)
    new = mm.remember(MemoryClass.PROCEDURAL, "deploy with ./scripts/deploy.sh", importance=0.5, data={"supersedes": old.id})
    stored_old = mm.store.get_memory(old.id)
    assert stored_old.superseded_by == new.id
    assert new.version == old.version + 1
    ids = {r.id for r in mm.retrieve("deploy")}
    assert new.id in ids and old.id not in ids
    # superseded record is hidden but not deleted
    assert mm.store.get_memory(old.id) is not None
    assert mm.stats()["superseded"] == 1


# -- retrieval -------------------------------------------------------------------


def test_retrieval_relevant_ranks_above_irrelevant(mm):
    rel = mm.remember(MemoryClass.SEMANTIC, "Python uses indentation to delimit blocks", tags=["python"], importance=0.6)
    irr = mm.remember(MemoryClass.SEMANTIC, "The Eiffel tower is in Paris", tags=["travel"], importance=0.9, confidence=0.9)
    partial = mm.remember(MemoryClass.SEMANTIC, "Python packaging uses pyproject.toml", tags=["python"], importance=0.6)
    got = mm.retrieve("python indentation")
    ids = [r.id for r in got]
    assert ids[0] == rel.id
    assert partial.id in ids
    assert irr.id not in ids


def test_retrieval_weights_confidence_importance_and_recency(mm):
    weak = mm.remember(MemoryClass.SEMANTIC, "gateway timeout defaults to 30 seconds", confidence=0.3, importance=0.4)
    strong = mm.remember(MemoryClass.SEMANTIC, "gateway timeout defaults to 60 seconds", confidence=0.95, importance=0.9)
    ids = [r.id for r in mm.retrieve("gateway timeout")]
    assert ids[0] == strong.id and weak.id in ids
    # recency: an old record loses to a fresh one with identical weights
    fresh = mm.remember(MemoryClass.SEMANTIC, "gateway retries default to 3", confidence=0.6, importance=0.6)
    stale = mm.remember(MemoryClass.SEMANTIC, "gateway retries default to 5", confidence=0.6, importance=0.6)
    stale.updated_at = _iso(datetime.now(timezone.utc) - timedelta(days=120))
    mm.store._conn.execute("UPDATE memories SET data_json=? WHERE id=?", (stale.model_dump_json(), stale.id))
    ids = [r.id for r in mm.retrieve("gateway retries default")]
    assert ids.index(fresh.id) < ids.index(stale.id)


def test_retrieval_mission_bonus_and_class_filter(mm):
    other = mm.remember(MemoryClass.EPISODIC, "ran the migration script successfully", mission_id="m2", importance=0.6)
    mine = mm.remember(MemoryClass.EPISODIC, "ran the migration script successfully twice", mission_id="m1", importance=0.6)
    ids = [r.id for r in mm.retrieve("migration script", mission_id="m1")]
    assert ids[0] == mine.id and other.id in ids
    assert mm.retrieve("migration script", classes=[MemoryClass.SEMANTIC]) == []
    assert [r.id for r in mm.retrieve("migration script", mission_id="m1", classes=[MemoryClass.EPISODIC], limit=1)] == [mine.id]
    # without a mission the two tie on score and fall back to id order (deterministic)
    assert [r.id for r in mm.retrieve("migration script", limit=1)] == [min(mine.id, other.id)]


def test_retrieval_by_tag_only(mm):
    rec = mm.remember(MemoryClass.SEMANTIC, "prefer explicit encoding in open calls", tags=["ruff"], importance=0.6)
    assert [r.id for r in mm.retrieve("ruff")] == [rec.id]


def test_retrieval_updates_access_counters(mm):
    rec = mm.remember(MemoryClass.SEMANTIC, "sqlite journal mode is WAL", importance=0.6)
    now = "2030-01-01T00:00:00.000+00:00"
    mm.retrieve("sqlite journal", now=now)
    mm.retrieve("sqlite journal", now=now)
    stored = mm.store.get_memory(rec.id)
    assert stored.access_count == 2
    assert stored.last_accessed_at == now


def test_retrieval_is_deterministic(mm):
    for i in range(6):
        mm.remember(MemoryClass.SEMANTIC, f"cluster node {i} runs the scheduler", importance=0.6, confidence=0.6)
    first = [r.id for r in mm.retrieve("cluster node scheduler", limit=4)]
    second = [r.id for r in mm.retrieve("cluster node scheduler", limit=4)]
    third = [r.id for r in mm.retrieve("cluster node scheduler", limit=4)]
    assert first == second == third and len(first) == 4
    assert first == sorted(first)  # equal scores fall back to id order


# -- working memory scoping --------------------------------------------------------


def test_working_memory_scoped_to_mission(mm):
    w1 = mm.remember(MemoryClass.WORKING, "current step: parse the invoice", mission_id="m1", importance=0.6)
    w2 = mm.remember(MemoryClass.WORKING, "current step: parse the ledger", mission_id="m2", importance=0.6)
    sem = mm.remember(MemoryClass.SEMANTIC, "parse step uses the invoice schema", importance=0.6)
    ids_m1 = {r.id for r in mm.retrieve("current step parse", mission_id="m1")}
    assert w1.id in ids_m1 and w2.id not in ids_m1 and sem.id in ids_m1
    ids_none = {r.id for r in mm.retrieve("current step parse")}
    assert w1.id not in ids_none and w2.id not in ids_none and sem.id in ids_none


# -- expiry ------------------------------------------------------------------------


def test_expiry_excluded_from_retrieval_and_deleted(mm):
    past = _iso(datetime.now(timezone.utc) - timedelta(days=1))
    future = _iso(datetime.now(timezone.utc) + timedelta(days=1))
    dead = mm.write(MemoryRecord(memory_class=MemoryClass.EPISODIC, content="token refresh scheduled", importance=0.6, expires_at=past))
    alive = mm.write(MemoryRecord(memory_class=MemoryClass.EPISODIC, content="token refresh completed", importance=0.6, expires_at=future))
    ids = {r.id for r in mm.retrieve("token refresh")}
    assert alive.id in ids and dead.id not in ids
    assert mm.expire() == 1
    assert mm.store.get_memory(dead.id) is None
    assert mm.store.get_memory(alive.id) is not None
    assert mm.expire() == 0


def test_expire_with_explicit_now(mm):
    rec = mm.write(MemoryRecord(memory_class=MemoryClass.EPISODIC, content="temporary note", importance=0.6, expires_at="2030-01-01T00:00:00.000+00:00"))
    assert mm.expire(now="2029-12-31T00:00:00.000+00:00") == 0
    assert mm.expire(now="2030-01-02T00:00:00.000+00:00") == 1
    assert mm.store.get_memory(rec.id) is None


# -- consolidation ---------------------------------------------------------------


def test_consolidate_drops_working_of_completed_missions(mm):
    mm.remember(MemoryClass.WORKING, "scratch a", mission_id="done", importance=0.6)
    mm.remember(MemoryClass.WORKING, "scratch b", mission_id="done", importance=0.6)
    keep = mm.remember(MemoryClass.WORKING, "scratch c", mission_id="live", importance=0.6)
    sem = mm.remember(MemoryClass.SEMANTIC, "durable fact", mission_id="done", importance=0.6)
    counts = mm.consolidate(completed_mission_ids={"done"})
    assert counts["dropped_working"] == 2
    assert mm.store.get_memory(keep.id) is not None
    assert mm.store.get_memory(sem.id) is not None
    assert mm.stats()["working"] == 1


def test_consolidate_uses_mission_status_from_store(mm, store):
    finished = store.save_mission(MissionState(objective="x", status=MissionStatus.COMPLETE))
    running = store.save_mission(MissionState(objective="y", status=MissionStatus.ACTIVE))
    mm.remember(MemoryClass.WORKING, "scratch finished", mission_id=finished.mission_id, importance=0.6)
    keep = mm.remember(MemoryClass.WORKING, "scratch running", mission_id=running.mission_id, importance=0.6)
    counts = mm.consolidate()
    assert counts["dropped_working"] == 1
    assert mm.store.get_memory(keep.id) is not None


def test_consolidate_merges_near_duplicate_semantic(mm):
    lo = mm.remember(MemoryClass.SEMANTIC, "project build pipeline uses pytest ruff mypy checks", confidence=0.5, importance=0.6, tags=["ci"])
    hi = mm.remember(MemoryClass.SEMANTIC, "project build pipeline uses pytest ruff mypy checks daily", confidence=0.9, importance=0.6, tags=["build"])
    far = mm.remember(MemoryClass.SEMANTIC, "project build pipeline is documented in the wiki", confidence=0.9, importance=0.6)
    assert mm.contradictions() == []
    counts = mm.consolidate()
    assert counts["merged"] == 1
    assert mm.store.get_memory(lo.id).superseded_by == hi.id
    winner = mm.store.get_memory(hi.id)
    assert winner.superseded_by is None
    assert winner.tags == ["build", "ci"]
    assert winner.version == 2
    assert winner.data["merged_from"] == [lo.id]
    assert mm.store.get_memory(far.id).superseded_by is None
    assert mm.stats()["semantic"] == 2
    # idempotent
    assert mm.consolidate()["merged"] == 0


def test_consolidate_promotes_episodic_to_semantic(mm):
    hot = mm.remember(MemoryClass.EPISODIC, "customer asked for CSV export of the ledger", importance=0.9, tags=["ledger"])
    cold = mm.remember(MemoryClass.EPISODIC, "customer asked for PDF export of the ledger", importance=0.9)
    rare = mm.remember(MemoryClass.EPISODIC, "customer asked for XML export of the ledger", importance=0.3)
    mm.retrieve("CSV export ledger", limit=1)
    mm.retrieve("CSV export ledger", limit=1)
    mm.retrieve("PDF export ledger", limit=1)  # cold accessed only once
    assert rare is None
    counts = mm.consolidate()
    assert counts["promoted"] == 1
    semantic = mm.store.all_memories(memory_class=MemoryClass.SEMANTIC)
    assert len(semantic) == 1
    promoted = semantic[0]
    assert promoted.content == hot.content
    assert promoted.data == {"consolidated_from": hot.id}
    assert promoted.tags == ["ledger"]
    assert mm.store.get_memory(hot.id).data["promoted_to"] == promoted.id
    assert mm.store.get_memory(cold.id).superseded_by is None
    # not promoted twice
    assert mm.consolidate()["promoted"] == 0
    assert len(mm.store.all_memories(memory_class=MemoryClass.SEMANTIC)) == 1


def test_consolidate_returns_all_counts(mm):
    assert mm.consolidate() == {"dropped_working": 0, "merged": 0, "promoted": 0}


# -- forget / stats -------------------------------------------------------------------


def test_forget(mm):
    rec = mm.remember(MemoryClass.META, "retrieval budget is small", importance=0.6)
    mm.forget(rec.id)
    assert mm.store.get_memory(rec.id) is None
    assert mm.stats()["meta"] == 0


def test_stats_counts_per_class(mm):
    mm.remember(MemoryClass.SEMANTIC, "fact one", importance=0.6)
    mm.remember(MemoryClass.SEMANTIC, "fact two", importance=0.6)
    mm.remember(MemoryClass.EPISODIC, "event one", importance=0.6)
    mm.remember(MemoryClass.FAILURE, "failure one", importance=0.0)
    mm.remember(MemoryClass.WORKING, "scratch", mission_id="m1", importance=0.6)
    s = mm.stats()
    for cls in MemoryClass:
        assert cls.value in s
    assert s["semantic"] == 2 and s["episodic"] == 1 and s["failure"] == 1 and s["working"] == 1
    assert s["causal"] == 0
    assert s["total"] == 5
    assert s["superseded"] == 0
