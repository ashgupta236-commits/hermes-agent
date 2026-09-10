"""Temporal truth maintenance (live-run incident, area A/B).

The live mission recorded an ESTABLISHED claim in cycle 1 — "calc.py and test_calc.py do not exist
yet" — then created those files. The claim did not become false; it became historical. The belief
graph treated it as an eternal proposition, produced a severity-1.0 contradiction against "both
deliverables now exist", and the controller issued `must_falsify` on cycles 5, 7 and 8. Those three
cycles cost $9.28 — 48% of the mission budget — re-litigating a question a `list_dir` answers in a
millisecond.

These tests pin the repaired behaviour: ABSENT(t0) -> CREATED(t1) -> PRESENT(t2) is a supersession,
not a contradiction; history stays queryable; and the settlement survives checkpoint/resume.
"""

from __future__ import annotations

from pathlib import Path

from cogos.beliefs import BeliefGraph
from cogos.beliefs.temporal import (
    TemporalSettler,
    asserts_current_state,
    classify_claim,
    subjects_of,
    supersede,
    supersede_overtaken,
)
from cogos.evaluation.support import Sandbox
from cogos.schemas.beliefs import Claim, ClaimStatus, Contradiction
from cogos.schemas.mission import MissionState


# -- subject extraction is general, not domain-specific ---------------------------------------


def test_subjects_are_extracted_from_path_shape_not_a_filename_list():
    live = "Baseline file set of /tmp/cogos-live-run is {README.md, REQUIREMENTS.md}; calc.py and test_calc.py do not exist yet"
    assert subjects_of(live) == ["README.md", "REQUIREMENTS.md", "calc.py", "test_calc.py"]
    # A different domain entirely — nothing here knows about Python projects.
    assert subjects_of("the export at /srv/data/quarterly.csv is absent") == ["quarterly.csv"]
    assert subjects_of("revenue grew 5 percent") == []


def test_only_propositions_about_current_state_are_time_scoped():
    assert asserts_current_state("calc.py and test_calc.py do not exist yet") is True
    assert asserts_current_state("/srv/data/quarterly.csv is missing") is True
    # Mentioning a file is not the same as asserting its current state — no over-reach.
    assert asserts_current_state("calc.py should use round() for rounding") is False
    assert asserts_current_state("the market is growing at 5 percent per year") is False


# -- the live sequence: ABSENT(t0) -> CREATED(t1) -> PRESENT(t2) -------------------------------


def test_a_later_observation_supersedes_the_earlier_one_instead_of_contradicting_it():
    state = MissionState(objective="Build the feature")
    absent = Claim(
        proposition="calc.py and test_calc.py do not exist yet",
        status=ClaimStatus.ESTABLISHED,
        confidence=0.91,
        observed_at="2026-09-10T17:53:00+00:00",
    )
    state.claims.append(absent)

    present = Claim(
        proposition="Both deliverables (/tmp/run/calc.py and /tmp/run/test_calc.py) now exist on disk",
        status=ClaimStatus.ESTABLISHED,
        confidence=0.98,
        observed_at="2026-09-10T18:04:00+00:00",
    )
    state.claims.append(present)

    overtaken = supersede_overtaken(state, present)

    assert [c.id for c in overtaken] == [absent.id]
    assert absent.superseded_by == present.id
    assert absent.status is ClaimStatus.STALE
    assert absent.live() is False
    assert present.live() is True
    # History is preserved, not deleted.
    assert absent in state.claims
    assert any("superseded" in a for a in absent.assumptions)


def test_a_superseded_claim_cannot_produce_a_live_contradiction():
    state = MissionState(objective="Build the feature")
    absent = Claim(proposition="output.csv does not exist", status=ClaimStatus.ESTABLISHED, confidence=0.9, decision_relevance=0.9)
    present = Claim(proposition="output.csv now exists on disk", status=ClaimStatus.ESTABLISHED, confidence=0.95, decision_relevance=0.9)
    state.claims.extend([absent, present])
    # Mark them mutually exclusive, which is what drove the live severity-1.0 contradiction.
    absent.assumptions.append(f"exclusive_with:{present.id}")

    graph = BeliefGraph(state)
    graph.detect_contradictions()
    assert state.unresolved_contradictions(), "while both are live the dispute is real"

    for c in state.contradictions:
        c.resolved = True
    supersede(absent, present, reason="later observation")

    graph2 = BeliefGraph(state)
    graph2.detect_contradictions()
    assert state.unresolved_contradictions() == [], "a superseded claim is history, not a competing account"
    assert absent in state.claims and absent.superseded_by == present.id


def test_falsification_never_targets_a_superseded_claim():
    state = MissionState(objective="Build the feature")
    stale = Claim(proposition="config.yaml is missing", status=ClaimStatus.ESTABLISHED, confidence=0.9, decision_relevance=0.9)
    fresh = Claim(proposition="config.yaml now exists", status=ClaimStatus.ESTABLISHED, confidence=0.9, decision_relevance=0.9)
    state.claims.extend([stale, fresh])

    graph = BeliefGraph(state)
    assert stale.id in [t.get("claim_id") for t in graph.falsification_targets(limit=5)]

    supersede(stale, fresh, reason="later observation")
    graph2 = BeliefGraph(state)
    targets = [t.get("claim_id") for t in graph2.falsification_targets(limit=5)]
    assert stale.id not in targets, "the controller must not spend falsification on settled history"
    assert fresh.id in targets


# -- deterministic settlement through the real tool fabric --------------------------------------


def _live_shaped_state(root: Path) -> tuple[MissionState, Claim, Claim, Contradiction]:
    state = MissionState(objective="Build the feature described in REQUIREMENTS.md.")
    state.resources["root"] = str(root)
    absent = Claim(
        proposition="Baseline file set is {README.md}; calc.py and test_calc.py do not exist yet",
        status=ClaimStatus.ESTABLISHED,
        confidence=0.91,
        decision_relevance=0.9,
        observed_at="2026-09-10T17:53:00+00:00",
    )
    present = Claim(
        proposition="Both deliverables calc.py and test_calc.py now exist on disk",
        status=ClaimStatus.ESTABLISHED,
        confidence=0.98,
        decision_relevance=0.9,
        observed_at="2026-09-10T18:04:00+00:00",
    )
    state.claims.extend([absent, present])
    contradiction = Contradiction(
        claim_ids=[absent.id, present.id],
        description="Conflicting evidence for: calc.py and test_calc.py do not exist yet",
        severity=1.0,
    )
    state.contradictions.append(contradiction)
    return state, absent, present, contradiction


def test_the_live_contradiction_is_settled_by_looking_not_by_deliberating():
    sb = Sandbox("temporal-settle")
    try:
        (sb.root / "README.md").write_text("# demo\n", encoding="utf-8")
        (sb.root / "calc.py").write_text("def add_percent(v, p): return round(v * (1 + p / 100), 2)\n", encoding="utf-8")
        (sb.root / "test_calc.py").write_text("def test_x(): assert True\n", encoding="utf-8")
        state, absent, present, contradiction = _live_shaped_state(sb.root)

        settler = TemporalSettler(sb.runtime.fabric, sb.runtime.tracer)
        result = settler.settle(contradiction, state, roots=[str(sb.root)])

        assert result.settled is True
        assert result.observed == {"README.md": True, "calc.py": True, "test_calc.py": True}
        assert contradiction.resolved is True
        assert contradiction.suspected_cause == "time_period"
        assert "earlier moment" in contradiction.resolution
        assert absent.id in result.superseded and absent.status is ClaimStatus.STALE
        assert present.live() is True, "the current-state claim survives"
        assert absent in state.claims, "history is preserved"
    finally:
        sb.cleanup()


def test_a_genuine_dispute_is_not_settled_away():
    """Settlement must only close disputes the world actually answers."""
    sb = Sandbox("temporal-genuine")
    try:
        state = MissionState(objective="Decide the rounding mode")
        a = Claim(proposition="round() satisfies the rounding requirement", status=ClaimStatus.ESTABLISHED, confidence=0.9)
        b = Claim(proposition="Decimal ROUND_HALF_UP is required instead", status=ClaimStatus.CONTESTED, confidence=0.6)
        state.claims.extend([a, b])
        contradiction = Contradiction(claim_ids=[a.id, b.id], description="rounding mode disputed", severity=0.9)
        state.contradictions.append(contradiction)

        result = TemporalSettler(sb.runtime.fabric, sb.runtime.tracer).settle(contradiction, state, roots=[str(sb.root)])
        assert result.settled is False
        assert contradiction.resolved is False, "a semantic dispute still escalates"
        assert a.live() and b.live()
    finally:
        sb.cleanup()


def test_a_value_change_in_the_environment_supersedes_rather_than_contradicts():
    """Not filesystem-specific: any observed current-state claim about the same subject."""
    state = MissionState(objective="Track the export")
    old = Claim(proposition="export.csv contains 1042 rows", status=ClaimStatus.ESTABLISHED, confidence=0.9, observed_at="2026-01-01T00:00:00+00:00")
    new = Claim(proposition="export.csv currently contains 2091 rows", status=ClaimStatus.ESTABLISHED, confidence=0.95, observed_at="2026-01-02T00:00:00+00:00")
    state.claims.extend([old, new])

    overtaken = supersede_overtaken(state, new)
    assert [c.id for c in overtaken] == [old.id]
    assert old.superseded_by == new.id and new.live()


def test_an_older_observation_never_supersedes_a_newer_one():
    state = MissionState(objective="Track the export")
    newer = Claim(proposition="export.csv is present", status=ClaimStatus.ESTABLISHED, confidence=0.9, observed_at="2026-01-02T00:00:00+00:00")
    older = Claim(proposition="export.csv is missing", status=ClaimStatus.ESTABLISHED, confidence=0.9, observed_at="2026-01-01T00:00:00+00:00")
    state.claims.extend([newer, older])

    assert supersede_overtaken(state, older) == [], "a stale observation cannot overwrite a fresh one"
    assert newer.live() and older.live()


# -- durability across checkpoint / resume ------------------------------------------------------


def test_a_settled_temporal_contradiction_does_not_revive_after_resume():
    """The live loop would have recurred on resume if resolution were not durable."""
    sb = Sandbox("temporal-resume")
    try:
        (sb.root / "calc.py").write_text("x = 1\n", encoding="utf-8")
        (sb.root / "test_calc.py").write_text("x = 1\n", encoding="utf-8")
        state, absent, present, contradiction = _live_shaped_state(sb.root)
        state.objective = "Build the feature"
        mission = sb.runtime.new_mission(state.objective, context={"root": str(sb.root)})
        mission.claims.extend([absent, present])
        mission.contradictions.append(contradiction)
        mission.resources["root"] = str(sb.root)

        TemporalSettler(sb.runtime.fabric, sb.runtime.tracer).settle(contradiction, mission, roots=[str(sb.root)])
        assert contradiction.resolved is True
        sb.runtime.store.save_mission(mission, "settled")

        sb.reopen()
        reloaded = sb.runtime.store.load_mission(mission.mission_id)
        assert reloaded is not None
        assert reloaded.unresolved_contradictions() == [], "the resolution survived the round trip"
        revived = [c for c in reloaded.claims if c.id == absent.id]
        assert revived and revived[0].status is ClaimStatus.STALE
        # And re-running detection must not resurrect it.
        BeliefGraph(reloaded).detect_contradictions()
        assert reloaded.unresolved_contradictions() == []
    finally:
        sb.cleanup()


def test_classify_claim_is_idempotent():
    claim = Claim(proposition="report.md does not exist")
    first = classify_claim(claim).model_copy(deep=True)
    second = classify_claim(claim)
    assert second.subjects == first.subjects
    assert second.observes_current_state == first.observes_current_state
    assert second.observed_at == first.observed_at
