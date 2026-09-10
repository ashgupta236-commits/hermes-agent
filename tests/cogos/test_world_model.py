"""Tests for the cogos world model manager."""

from __future__ import annotations

from cogos.schemas.cognition import CausalUpdateSpec, WorldUpdateSpec
from cogos.schemas.common import EpistemicStatus, Provenance
from cogos.schemas.world import WorldModel
from cogos.world_model import HISTORY_LIMIT, WorldModelManager


def _mgr() -> WorldModelManager:
    return WorldModelManager(WorldModel())


def _prov(source: str = "tool:probe") -> Provenance:
    return Provenance(source=source)


# --- entities ------------------------------------------------------------------


def test_upsert_entity_is_case_insensitive():
    m = _mgr()
    a = m.upsert_entity("Acme Corp", kind="organisation")
    b = m.upsert_entity("acme corp")
    assert a is b
    assert len(m.world.entities) == 1
    assert a.kind == "organisation"
    assert m.world.history[0]["event"] == "entity_added"


def test_upsert_entity_status_not_downgraded_or_silently_upgraded():
    m = _mgr()
    e = m.upsert_entity("Server", epistemic_status=EpistemicStatus.OBSERVATION)
    m.upsert_entity("server", epistemic_status=EpistemicStatus.ASSUMPTION)
    assert e.epistemic_status == EpistemicStatus.OBSERVATION
    assert m.world.history[-1]["event"] == "ignored_downgrade"
    # upgrade without provenance is ignored, with provenance it is applied and logged
    m.upsert_entity("server", epistemic_status=EpistemicStatus.ESTABLISHED_FACT)
    assert e.epistemic_status == EpistemicStatus.OBSERVATION
    m.upsert_entity("server", epistemic_status=EpistemicStatus.ESTABLISHED_FACT, provenance=_prov())
    assert e.epistemic_status == EpistemicStatus.ESTABLISHED_FACT
    assert m.world.history[-1]["event"] == "entity_status_upgraded"


# --- properties --------------------------------------------------------------------


def test_fact_not_downgraded_by_assumption():
    m = _mgr()
    fact = m.set_property("Acme", "revenue", 100, epistemic_status=EpistemicStatus.ESTABLISHED_FACT, confidence=0.95)
    kept = m.set_property("acme", "revenue", 50, epistemic_status=EpistemicStatus.ASSUMPTION)
    assert kept is fact
    assert kept.value == 100
    assert kept.epistemic_status == EpistemicStatus.ESTABLISHED_FACT
    entity = m.find_entity("Acme")
    assert entity is not None and len(entity.properties) == 1
    last = m.world.history[-1]
    assert last["event"] == "ignored_downgrade"
    assert last["ignored"] == "assumption" and last["ignored_value"] == 50
    # hypothesis is ignored the same way
    m.set_property("acme", "revenue", 60, epistemic_status=EpistemicStatus.HYPOTHESIS)
    assert m.current_property("acme", "revenue") is fact


def test_differing_statuses_keep_both_versions():
    m = _mgr()
    assumed = m.set_property("Acme", "hq", "Berlin", epistemic_status=EpistemicStatus.ASSUMPTION, confidence=0.4)
    observed = m.set_property("Acme", "hq", "Munich", epistemic_status=EpistemicStatus.OBSERVATION, provenance=_prov())
    assert assumed.valid_to is not None
    assert observed.valid_to is None
    assert m.current_property("acme", "hq") is observed
    entity = m.find_entity("acme")
    assert entity is not None and len(entity.properties) == 2
    assert m.world.history[-1]["event"] == "property_versioned"


def test_same_status_same_value_confirms_and_different_value_versions():
    m = _mgr()
    p = m.set_property("Acme", "employees", 10, confidence=0.6)
    again = m.set_property("Acme", "employees", 10, confidence=0.9)
    assert again is p and p.confidence == 0.9
    changed = m.set_property("Acme", "employees", 12)
    assert changed is not p and p.valid_to is not None and changed.valid_to is None
    assert m.current_property("acme", "employees") is changed


def test_weaker_duplicate_value_is_ignored():
    m = _mgr()
    obs = m.set_property("Acme", "colour", "red", epistemic_status=EpistemicStatus.OBSERVATION)
    same = m.set_property("Acme", "colour", "red", epistemic_status=EpistemicStatus.ASSUMPTION)
    assert same is obs
    assert m.world.history[-1]["event"] == "ignored_weaker_duplicate"


# --- relations -------------------------------------------------------------------


def test_add_relation_dedupes_and_neighbors():
    m = _mgr()
    r1 = m.add_relation("Alice", "Acme", "works_at")
    r2 = m.add_relation("alice", "ACME", "works_at", confidence=0.9)
    assert r1 is r2
    assert r1.confidence == 0.9
    r3 = m.add_relation("Acme", "Bob", "employs")
    assert len(m.world.relations) == 2
    assert m.neighbors("acme") == [r1, r3]
    assert m.neighbors("alice") == [r1]
    assert m.neighbors("nobody") == []
    # status upgrade needs provenance
    m.add_relation("Alice", "Acme", "works_at", epistemic_status=EpistemicStatus.ESTABLISHED_FACT)
    assert r1.epistemic_status == EpistemicStatus.OBSERVATION
    m.add_relation("Alice", "Acme", "works_at", epistemic_status=EpistemicStatus.ESTABLISHED_FACT, provenance=_prov())
    assert r1.epistemic_status == EpistemicStatus.ESTABLISHED_FACT


# --- causal --------------------------------------------------------------------------


def test_causal_dedupe_merges_evidence_and_status():
    m = _mgr()
    a = m.add_causal("Rain", "Wet ground", mechanism="water falls", strength=0.6)
    assert a.epistemic_status == EpistemicStatus.HYPOTHESIS
    # stronger status without evidence: keep the weaker
    b = m.add_causal("rain", "WET GROUND", epistemic_status=EpistemicStatus.OBSERVATION)
    assert b is a
    assert a.epistemic_status == EpistemicStatus.HYPOTHESIS
    assert len(m.world.causal_links) == 1
    # with evidence the stronger status is kept and evidence ids are merged
    c = m.add_causal("Rain", "Wet ground", strength=0.8, epistemic_status=EpistemicStatus.OBSERVATION, evidence_ids=["ev_1"])
    assert c is a
    assert a.evidence_ids == ["ev_1"]
    assert a.epistemic_status == EpistemicStatus.OBSERVATION
    assert a.strength == 0.8
    m.add_causal("Rain", "Wet ground", evidence_ids=["ev_1", "ev_2"], epistemic_status=EpistemicStatus.HYPOTHESIS)
    assert a.evidence_ids == ["ev_1", "ev_2"]
    assert a.epistemic_status == EpistemicStatus.OBSERVATION  # evidence supports the stronger one
    assert a.mechanism == "water falls"
    assert a.confidence > 0.7


def test_causal_chain():
    m = _mgr()
    m.add_causal("A", "B")
    m.add_causal("B", "C")
    m.add_causal("B", "D")
    m.add_causal("C", "A")  # cycle
    m.add_causal("D", "E")
    chains = m.causal_chain("a", depth=3)
    assert sorted(chains) == [["A", "B", "C"], ["A", "B", "D", "E"]]
    assert m.causal_chain("A", depth=1) == [["A", "B"]]
    assert m.causal_chain("E") == []
    assert m.causal_chain("unknown") == []


# --- predictions -------------------------------------------------------------------


def test_predictions():
    m = _mgr()
    p = m.add_prediction("Latency drops below 100ms", probability=0.7, horizon="1w", based_on_claim_ids=["clm_1"])
    assert p in m.world.predictions
    assert p.resolved is None
    assert m.resolve_prediction(p.id, True) is p
    assert p.resolved is True and p.outcome == "confirmed"
    assert m.resolve_prediction("prd_missing", False) is None
    q = m.add_prediction("x", probability=0.1)
    m.resolve_prediction(q.id, False, note="measured 150ms")
    assert q.resolved is False and q.outcome == "measured 150ms"


# --- specs & time ----------------------------------------------------------------


def test_apply_world_and_causal_updates():
    m = _mgr()
    spec = WorldUpdateSpec(
        entity="Postgres",
        kind="resource",
        property_name="version",
        property_value="16",
        relation_to="App",
        relation_kind="depends_on",
        epistemic_status=EpistemicStatus.OBSERVATION,
        confidence=0.9,
    )
    entity = m.apply(spec, provenance=_prov("tool:psql"))
    assert entity.kind == "resource"
    prop = m.current_property("postgres", "version")
    assert prop is not None and prop.value == "16" and prop.confidence == 0.9
    assert prop.provenance is not None and prop.provenance.source == "tool:psql"
    assert len(m.neighbors("app")) == 1
    link = m.apply_causal(CausalUpdateSpec(cause="Postgres", effect="App latency", mechanism="slow queries", strength=0.7))
    assert link in m.world.causal_links and link.epistemic_status == EpistemicStatus.HYPOTHESIS
    assert m.causal_chain("postgres") == [["Postgres", "App latency"]]


def test_advance_time():
    m = _mgr()
    stamp = m.advance_time("2026-01-01T00:00:00+00:00")
    assert m.world.temporal_now == stamp == "2026-01-01T00:00:00+00:00"
    later = m.advance_time()
    assert later > stamp
    assert m.world.history[-1]["event"] == "time_advanced"


# --- summary & history bound ---------------------------------------------------------


def test_snapshot_summary_has_status_tags():
    m = _mgr()
    m.set_property("Acme", "revenue", 100, epistemic_status=EpistemicStatus.ESTABLISHED_FACT)
    m.set_property("Acme", "hq", "Berlin", epistemic_status=EpistemicStatus.ASSUMPTION)
    m.upsert_entity("Ghost", kind="actor", epistemic_status=EpistemicStatus.HYPOTHESIS)
    lines = m.snapshot_summary()
    joined = "\n".join(lines)
    assert "revenue=100 (established_fact)" in joined
    assert "hq=Berlin (assumption)" in joined
    assert "Ghost [actor] (hypothesis)" in joined
    assert len(m.snapshot_summary(limit=1)) == 1


def test_history_is_bounded():
    m = _mgr()
    for i in range(HISTORY_LIMIT + 50):
        m.set_property("Counter", "n", i)
    assert len(m.world.history) == HISTORY_LIMIT
    assert m.world.history[-1]["new"] == HISTORY_LIMIT + 49
    # oversize history on load is trimmed too
    w = WorldModel(history=[{"event": "x", "i": i} for i in range(HISTORY_LIMIT + 10)])
    WorldModelManager(w)
    assert len(w.history) == HISTORY_LIMIT
    assert w.history[0]["i"] == 10
