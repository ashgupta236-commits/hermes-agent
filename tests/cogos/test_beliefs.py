"""Tests for the cogos belief graph."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cogos.beliefs import BeliefGraph, TournamentResult, normalise_proposition
from cogos.beliefs.graph import EXCLUSIVE_PREFIX
from cogos.schemas.beliefs import Claim, ClaimStatus, Evidence, EvidenceKind, Hypothesis
from cogos.schemas.common import EpistemicStatus, Provenance
from cogos.schemas.mission import MissionState


def _state() -> MissionState:
    return MissionState(objective="test")


def _ev(
    summary: str,
    source: str,
    supports: list[str] | None = None,
    contradicts: list[str] | None = None,
    kind: EvidenceKind = EvidenceKind.SECONDARY,
    reliability: float = 0.5,
    lineage: list[str] | None = None,
    weight: float = 0.5,
    scope: str = "",
    freshness: str | None = None,
) -> Evidence:
    return Evidence(
        summary=summary,
        supports_claim_ids=supports or [],
        contradicts_claim_ids=contradicts or [],
        kind=kind,
        provenance=Provenance(source=source, reliability=reliability, lineage=lineage or []),
        weight=weight,
        scope=scope,
        freshness=freshness,
    )


# --- claims --------------------------------------------------------------------


def test_normalise_proposition():
    assert normalise_proposition("  The  Sky is BLUE. ") == "the sky is blue"
    assert normalise_proposition("x...") == "x"


def test_add_claim_dedupes_and_merges():
    g = BeliefGraph(_state())
    a = g.add_claim(Claim(proposition="The sky is blue.", falsification_conditions=["look up"]))
    b = g.add_claim(Claim(proposition="the   SKY is blue", falsification_conditions=["photograph"], assumptions=["daytime"]))
    assert a is b
    assert len(g.state.claims) == 1
    assert a.falsification_conditions == ["look up", "photograph"]
    assert a.assumptions == ["daytime"]


def test_find_claim_by_id_exact_and_fuzzy():
    g = BeliefGraph(_state())
    c = g.add_claim(Claim(proposition="Revenue grew 20 percent in 2024"))
    assert g.find_claim(c.id) is c
    assert g.find_claim("revenue grew 20 percent in 2024.") is c
    assert g.find_claim("Revenue grew 20 percent during 2024") is c  # jaccard >= 0.6
    assert g.find_claim("completely unrelated statement about cats") is None


def test_new_claim_has_open_status_and_established_fact_status():
    g = BeliefGraph(_state())
    c = g.add_claim(Claim(proposition="hypothesis"))
    assert c.status == ClaimStatus.OPEN
    f = g.add_claim(Claim(proposition="fact", epistemic_status=EpistemicStatus.ESTABLISHED_FACT))
    assert f.status == ClaimStatus.ESTABLISHED
    assert f.epistemic_status == EpistemicStatus.ESTABLISHED_FACT


# --- evidence --------------------------------------------------------------------


def test_add_evidence_links_by_proposition_and_creates_missing_claim():
    g = BeliefGraph(_state())
    c = g.add_claim(Claim(proposition="The API rate limit is 100 per minute"))
    ev = g.add_evidence(_ev("docs say 100/min", "https://docs.python.org/x", supports=["the api rate limit is 100 per minute"]))
    assert ev.supports_claim_ids == [c.id]
    assert c.evidence_for == [ev.id]
    assert ev.provenance.reliability == 0.8  # from source_trust, default 0.5 replaced

    ev2 = g.add_evidence(_ev("brand new", "tool:grep", supports=["Latency is under 50ms"]))
    new_claim = g.find_claim("Latency is under 50ms")
    assert new_claim is not None
    assert new_claim.epistemic_status == EpistemicStatus.HYPOTHESIS
    assert ev2.supports_claim_ids == [new_claim.id]
    assert new_claim.evidence_for == [ev2.id]
    # unmatched contradicted propositions are dropped rather than invented
    ev3 = g.add_evidence(_ev("against nothing", "tool:x", contradicts=["something never claimed at all"]))
    assert ev3.contradicts_claim_ids == []


def test_add_evidence_keeps_explicit_reliability():
    g = BeliefGraph(_state())
    ev = g.add_evidence(_ev("x", "https://reddit.com/r/x", reliability=0.9))
    assert ev.provenance.reliability == 0.9


def test_derived_from_detects_shared_roots_and_same_source():
    g = BeliefGraph(_state())
    g.add_claim(Claim(proposition="P"))
    a = g.add_evidence(_ev("a", "https://site-a.com/story", supports=["P"], lineage=["https://origin.org/report"]))
    b = g.add_evidence(_ev("b", "https://site-b.com/story", supports=["P"], lineage=["https://origin.org/report"]))
    c = g.add_evidence(_ev("c", "https://www.site-a.com/story/", supports=["P"]))
    d = g.add_evidence(_ev("d", "https://independent.net/x", supports=["P"]))
    assert b.derived_from == [a.id]
    assert c.derived_from == [a.id]
    assert d.derived_from == []


def test_false_consensus_discount():
    # five repeats of one report
    g1 = BeliefGraph(_state())
    g1.add_claim(Claim(proposition="P"))
    for i in range(5):
        g1.add_evidence(_ev(f"repeat {i}", f"https://site{i}.com/a", supports=["P"], reliability=0.8, lineage=["report-x"]))
    repeated = g1.find_claim("P")

    # three independent sources
    g2 = BeliefGraph(_state())
    g2.add_claim(Claim(proposition="P"))
    for i in range(3):
        g2.add_evidence(_ev(f"indep {i}", f"https://indep{i}.org/a", supports=["P"], reliability=0.8))
    independent = g2.find_claim("P")

    assert repeated is not None and independent is not None
    assert repeated.source_independence == 1
    assert independent.source_independence == 3
    assert repeated.confidence < independent.confidence
    assert independent.source_quality == 0.8


def test_recompute_prior_and_clamp():
    g = BeliefGraph(_state())
    c = g.add_claim(Claim(proposition="P", confidence=0.2))
    g.recompute()
    assert c.confidence == 0.2  # no evidence: prior kept
    for i in range(12):
        g.add_evidence(_ev(f"e{i}", f"https://s{i}.gov/x", supports=["P"], kind=EvidenceKind.PRIMARY, reliability=0.9, weight=1.0))
    assert c.confidence <= 0.98
    assert c.status == ClaimStatus.ESTABLISHED
    assert c.epistemic_status == EpistemicStatus.HYPOTHESIS  # never silently upgraded


def test_status_transitions():
    g = BeliefGraph(_state())
    c = g.add_claim(Claim(proposition="P"))
    assert c.status == ClaimStatus.OPEN
    g.add_evidence(_ev("for1", "https://a.org/1", supports=["P"], reliability=0.8, weight=0.8))
    assert c.status == ClaimStatus.SUPPORTED
    g.add_evidence(_ev("for2", "https://b.org/1", supports=["P"], reliability=0.9, weight=1.0, kind=EvidenceKind.PRIMARY))
    assert c.status == ClaimStatus.ESTABLISHED
    assert c.source_independence == 2

    r = g.add_claim(Claim(proposition="Q"))
    g.add_evidence(_ev("against", "tests:unit", contradicts=["Q"], kind=EvidenceKind.PRIMARY, reliability=0.95, weight=1.0))
    assert r.status == ClaimStatus.REFUTED
    assert r.confidence <= 0.15

    k = g.add_claim(Claim(proposition="R"))
    g.add_evidence(_ev("for", "https://x.org/1", supports=["R"], reliability=0.8, weight=0.8))
    g.add_evidence(_ev("against", "https://y.org/1", contradicts=["R"], reliability=0.8, weight=0.8))
    assert k.status == ClaimStatus.CONTESTED
    assert abs(k.confidence - 0.5) < 0.25


# --- contradictions --------------------------------------------------------------


def test_detect_contradictions_with_scope_cause_and_resolution():
    g = BeliefGraph(_state())
    c = g.add_claim(Claim(proposition="Market share is 40 percent", decision_relevance=0.8))
    g.add_evidence(_ev("EU report", "https://a.org/eu", supports=["Market share is 40 percent"], reliability=0.8, weight=0.8, scope="EU"))
    g.add_evidence(_ev("US report", "https://b.org/us", contradicts=["Market share is 40 percent"], reliability=0.8, weight=0.8, scope="US"))
    found = g.detect_contradictions()
    assert len(found) == 1
    ctr = found[0]
    assert ctr.claim_ids == [c.id]
    assert ctr.suspected_cause == "scope"
    assert ctr.severity >= 0.8
    assert len(ctr.evidence_ids) == 2
    # dedupe on repeat
    assert len(g.detect_contradictions()) == 1
    assert len(g.state.contradictions) == 1
    assert g.serious_contradictions(0.5) == [ctr]
    g.resolve_contradiction(ctr.id, "EU vs US definitions", "definition")
    assert ctr.resolved and ctr.suspected_cause == "definition"
    assert g.detect_contradictions() == []
    assert g.serious_contradictions() == []


def test_contradiction_time_period_cause():
    g = BeliefGraph(_state())
    g.add_claim(Claim(proposition="Price is 10"))
    g.add_evidence(_ev("old", "https://a.org/1", supports=["Price is 10"], reliability=0.8, weight=0.8, freshness="2023-01-01"))
    g.add_evidence(_ev("new", "https://b.org/1", contradicts=["Price is 10"], reliability=0.8, weight=0.8, freshness="2025-01-01"))
    found = g.detect_contradictions()
    assert found[0].suspected_cause == "time_period"


def test_exclusive_claims_contradiction():
    g = BeliefGraph(_state())
    a = g.add_claim(Claim(proposition="Bug is in the parser", confidence=0.5))
    b = g.add_claim(Claim(proposition="Bug is in the network layer", confidence=0.5))
    g.declare_exclusive(a.id, b.id)
    assert f"{EXCLUSIVE_PREFIX}{b.id}" in a.assumptions
    assert f"{EXCLUSIVE_PREFIX}{a.id}" in b.assumptions
    assert g.detect_contradictions() == []  # neither believed yet
    g.add_evidence(_ev("trace", "tool:parser-test", supports=[a.id], kind=EvidenceKind.PRIMARY, reliability=0.9, weight=0.8))
    g.add_evidence(_ev("packet", "tool:tcpdump", supports=[b.id], kind=EvidenceKind.PRIMARY, reliability=0.9, weight=0.8))
    assert a.confidence >= 0.6 and b.confidence >= 0.6
    found = g.detect_contradictions()
    assert len(found) == 1
    assert sorted(found[0].claim_ids) == sorted([a.id, b.id])
    # a fresh graph on the same state rebuilds exclusivity from the markers
    g2 = BeliefGraph(g.state)
    assert len(g2.detect_contradictions()) == 1


def test_contradiction_against_established_claim():
    g = BeliefGraph(_state())
    c = g.add_claim(Claim(proposition="Water boils at 100C at sea level", epistemic_status=EpistemicStatus.ESTABLISHED_FACT, decision_relevance=0.6))
    assert c.status == ClaimStatus.ESTABLISHED
    g.add_evidence(_ev("blog says 90C", "https://x.blogspot.com/p", contradicts=[c.id], weight=0.3))
    assert c.status == ClaimStatus.ESTABLISHED  # epistemic status wins
    found = g.detect_contradictions()
    assert len(found) == 1
    assert found[0].suspected_cause == "source_error"


# --- staleness -----------------------------------------------------------------


def test_mark_stale():
    g = BeliefGraph(_state())
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    fresh_sensitive = g.add_claim(Claim(proposition="Stock price is 100", created_at=old))
    timeless = g.add_claim(Claim(proposition="Pi is irrational", created_at=old))
    g.add_evidence(_ev("quote", "https://q.org/1", supports=[fresh_sensitive.id], reliability=0.9, weight=1.0, freshness="2025-01-01"))
    g.add_evidence(_ev("proof", "https://p.org/1", supports=[timeless.id], reliability=0.9, weight=1.0))
    fresh_sensitive.last_verified_at = old
    timeless.last_verified_at = old
    before = fresh_sensitive.confidence
    stale = g.mark_stale(30)
    assert stale == [fresh_sensitive]
    assert fresh_sensitive.status == ClaimStatus.STALE
    assert abs(fresh_sensitive.confidence - (before + 0.3 * (0.5 - before))) < 1e-9
    assert timeless.status != ClaimStatus.STALE
    # explicit now in the past: nothing is stale
    g2 = BeliefGraph(_state())
    c = g2.add_claim(Claim(proposition="X", created_at=old, freshness="2025"))
    assert g2.mark_stale(30, now=(datetime.now(timezone.utc) - timedelta(days=39)).isoformat()) == []
    assert c.status == ClaimStatus.OPEN
    # a bare recompute keeps the stale status; new evidence re-verifies
    g.recompute()
    assert fresh_sensitive.status == ClaimStatus.STALE
    g.add_evidence(_ev("new quote", "https://r.org/1", supports=[fresh_sensitive.id], reliability=0.9, weight=1.0, freshness="2026-01-01"))
    assert fresh_sensitive.status != ClaimStatus.STALE
    assert fresh_sensitive.last_verified_at is not None and fresh_sensitive.last_verified_at > old


# --- tournament ------------------------------------------------------------------


def test_tournament_ranking_and_discriminating_predictions():
    st = _state()
    g = BeliefGraph(st)
    q = "Why did latency spike?"
    h1 = Hypothesis(question=q, statement="GC pauses", explanatory_power=0.8, prior=0.5,
                    unique_predictions=["GC logs show long pauses", "heap near limit"],
                    disconfirming_observations=["no GC pause > 100ms in logs"])
    h2 = Hypothesis(question=q, statement="Network congestion", explanatory_power=0.6, prior=0.5,
                    unique_predictions=["packet loss > 1%", "heap near limit"])
    h3 = Hypothesis(question=q, statement="Cosmic rays", explanatory_power=0.1, prior=0.2)
    st.hypotheses.extend([h1, h2, h3])
    e_for = g.add_evidence(_ev("gc log", "tool:gc-log", kind=EvidenceKind.PRIMARY, reliability=0.9, weight=0.8))
    e_against = g.add_evidence(_ev("no loss", "tool:ping", kind=EvidenceKind.PRIMARY, reliability=0.9, weight=0.5))
    e_kill = g.add_evidence(_ev("shielding", "tests:physics", kind=EvidenceKind.PRIMARY, reliability=0.95, weight=1.0))
    h1.supporting_evidence.append(e_for.id)
    h2.contradicting_evidence.append(e_against.id)
    h3.contradicting_evidence.append(e_kill.id)

    res = g.tournament(q)
    assert isinstance(res, TournamentResult)
    assert res.leading_id == h1.id
    assert [r["hypothesis_id"] for r in res.ranked] == [h1.id, h2.id, h3.id]
    assert res.ranked[0]["score"] > res.ranked[1]["score"]
    assert 0 < res.margin < 1
    assert h1.status == "leading" and h2.status == "active" and h3.status == "eliminated"
    assert h3.confidence < 0.1
    assert set(res.discriminating_predictions) == {"GC logs show long pauses", "packet loss > 1%"}
    assert "heap near limit" not in res.discriminating_predictions
    assert res.recommended_falsification == "no GC pause > 100ms in logs"
    # question filtering: unrelated question has no hypotheses
    assert g.tournament("Is the moon made of cheese?").ranked == []


def test_tournament_single_hypothesis_is_trivial():
    st = _state()
    g = BeliefGraph(st)
    h = Hypothesis(question="q", statement="only one", unique_predictions=["p"], disconfirming_observations=["d"])
    st.hypotheses.append(h)
    res = g.tournament("q")
    assert res.leading_id == h.id
    assert res.margin == 1.0
    assert res.discriminating_predictions == []
    assert res.recommended_falsification == "d"
    assert h.status == "leading"
    assert g.tournament("nothing").leading_id is None


# --- falsification targets & summary --------------------------------------------


def test_falsification_targets():
    st = _state()
    g = BeliefGraph(st)
    strong = g.add_claim(Claim(proposition="Strong relevant", confidence=0.9, decision_relevance=0.9, falsification_conditions=["run test A"]))
    g.add_claim(Claim(proposition="Strong irrelevant", confidence=0.9, decision_relevance=0.2))
    g.add_claim(Claim(proposition="Weak relevant", confidence=0.4, decision_relevance=0.9))
    h = Hypothesis(question="q", statement="lead", prior=0.7, disconfirming_observations=["obs"], claim_id=strong.id)
    st.hypotheses.append(h)
    g.tournament("q")
    targets = g.falsification_targets(limit=3)
    ids = [t.get("claim_id") or t.get("hypothesis_id") for t in targets]
    assert strong.id in ids and h.id in ids
    assert len(targets) == 2
    assert targets[0]["priority"] >= targets[1]["priority"]
    claim_target = next(t for t in targets if t.get("claim_id") == strong.id)
    assert claim_target["falsification_conditions"] == ["run test A"]
    assert abs(claim_target["priority"] - 0.81) < 1e-6
    hyp_target = next(t for t in targets if t.get("hypothesis_id") == h.id)
    assert hyp_target["disconfirming_observations"] == ["obs"]
    assert g.falsification_targets(limit=1) == targets[:1]


def test_summary_for_workspace():
    g = BeliefGraph(_state())
    g.add_claim(Claim(proposition="low", decision_relevance=0.1))
    g.add_claim(Claim(proposition="high", decision_relevance=0.9, confidence=0.82))
    g.add_evidence(_ev("e", "https://a.org/1", supports=["high"], reliability=0.8, weight=0.8))
    lines = g.summary_for_workspace(limit=1)
    assert len(lines) == 1
    assert lines[0].startswith("[supported ")
    assert lines[0].endswith(" 1 indep] high")
    assert len(g.summary_for_workspace()) == 2
