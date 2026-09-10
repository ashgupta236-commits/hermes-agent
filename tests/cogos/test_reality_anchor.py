"""R1 acceptance tests: the blind protocol, holds, and bounded resolution.

These use benign controlled environments — a stale label, contradictory timestamps, wrong units,
a renamed workspace, a deleted output — and a scripted anchor. That is deliberate and bounded:
a scripted anchor establishes what the *protocol* does. It establishes nothing about whether a
real model's bias is reduced by blinding, which requires the real adapter (see
`test_isolation_metadata_records_its_own_limits`) and a comparative evaluation.

Numbering follows the R1 acceptance list in the upgrade brief.
"""

from __future__ import annotations

import pytest

from cogos.schemas.anchor import (
    AnchorAssessment,
    AnchorVerdict,
    BeliefSnapshot,
    DisagreementKind,
    HoldStatus,
    IsolationMetadata,
    Observation,
    ObservationScope,
    ResolutionReceipt,
    stable_hash,
)
from cogos.schemas.common import TrustLevel
from cogos.schemas.mission import MissionState, Task, TaskStatus
from cogos.planner import Planner
from cogos.verification.reality_anchor import (
    MAX_RESOLUTION_ROUNDS,
    BlindnessViolation,
    ObservationCollector,
    RealityAnchor,
    SnapshotBuilder,
    compare,
    deterministic_anchor,
    dispatch_allowed,
    open_hold,
    render_packet,
    resolve_hold,
    validate_assessment,
)


def _collector() -> ObservationCollector:
    return ObservationCollector(mission_id="msn_test", environment_id="/work/project-a")


def _obs(collector, content, **kw) -> Observation:
    kw.setdefault("source", "sensor")
    return collector.capture(content, **kw)


def _isolated() -> IsolationMetadata:
    return IsolationMetadata(model="scripted-anchor", adapter="test", fresh_session=True, known_limitations=["scripted anchor: protocol behaviour only"])


def _belief(proposition: str, conditions: list[str]) -> BeliefSnapshot:
    return BeliefSnapshot(branch="mission", proposition=proposition, validity_conditions=conditions)


# -- 1. identical evidence, opposite commitments, identical packet --------------------------


def test_opposite_commitments_produce_an_identical_blind_packet():
    c = _collector()
    observations = [
        _obs(c, "row_count=1042", reference="dataset/rows", scope=ObservationScope.ENVIRONMENT, units="rows"),
        _obs(c, "label=stale-2024", reference="dataset/label", scope=ObservationScope.ENVIRONMENT),
    ]
    builder = SnapshotBuilder()
    props = ["The dataset is current"]

    optimistic = _belief("The dataset is current and the pipeline can ship", props)
    pessimistic = _belief("The dataset is stale and shipping would be wrong", props)

    s1 = builder.build(observations, "Is the dataset current?", propositions=props)
    s2 = builder.build(observations, "Is the dataset current?", propositions=props)
    p1, p2 = render_packet(s1, observations), render_packet(s2, observations)

    assert p1["observations"] == p2["observations"]
    assert s1.content_hash == s2.content_hash, "the same evidence and question produce the same sealed packet"
    for packet, belief in ((p1, optimistic), (p2, pessimistic)):
        rendered = str(packet)
        assert "can ship" not in rendered and "shipping would be wrong" not in rendered
        RealityAnchor(lambda _p: {"verdict": "inconclusive"}, _isolated()).assess(s1, observations, belief)


def test_a_packet_carrying_the_executives_conclusion_is_refused():
    c = _collector()
    belief = _belief("The migration completed successfully with no data loss", ["No rows were lost"])
    leaked = [_obs(c, "engineer note: the migration completed successfully with no data loss", reference="notes/1")]
    snapshot = SnapshotBuilder().build(leaked, "Did the migration lose rows?", propositions=["No rows were lost"])
    with pytest.raises(BlindnessViolation):
        RealityAnchor(lambda _p: {"verdict": "supported"}, _isolated()).assess(snapshot, leaked, belief)


# -- 2. interpretive context survives blinding ----------------------------------------------


def test_units_and_environment_identity_reach_the_anchor():
    c = _collector()
    observations = [_obs(c, "latency=850", reference="metrics/latency", units="milliseconds", scope=ObservationScope.ENVIRONMENT)]
    snapshot = SnapshotBuilder().build(
        observations,
        "Is latency within the 1 second budget?",
        propositions=["Latency is within budget"],
        definitions=["budget is 1000 milliseconds", "environment: /work/project-a"],
    )
    packet = render_packet(snapshot, observations)
    assert packet["observations"][0]["units"] == "milliseconds"
    assert packet["observations"][0]["environment_id"] == "/work/project-a"
    assert any("1000 milliseconds" in d for d in packet["definitions"])


def test_missing_context_yields_unknown_not_fabricated_certainty():
    c = _collector()
    observations = [_obs(c, "the deployment ran", reference="log/1")]
    snapshot = SnapshotBuilder().build(observations, "Did revenue increase?", propositions=["Revenue increased after the deployment"])
    verdict = deterministic_anchor(render_packet(snapshot, observations))
    assert verdict["verdict"] == "inconclusive"
    assert verdict["unknown"] == ["Revenue increased after the deployment"]
    assert verdict["missing_information"]


# -- 3. contradictory observations reach the anchor ------------------------------------------


def test_contradictory_observations_are_not_dropped_from_the_packet():
    c = _collector()
    observations = [
        _obs(c, "measured_at=2026-01-01T00:00:00Z value=5", reference="sensor/a", units="units"),
        _obs(c, "measured_at=2026-01-01T00:00:00Z value=9", reference="sensor/b", units="units"),
    ]
    snapshot = SnapshotBuilder().build(observations, "What is the value?", propositions=["The value is 5"])
    packet = render_packet(snapshot, observations)
    contents = [o["content"] for o in packet["observations"]]
    assert any("value=5" in x for x in contents) and any("value=9" in x for x in contents)
    assert snapshot.omitted == []


def test_a_superseded_observation_is_omitted_with_its_reason_recorded():
    c = _collector()
    first = _obs(c, "status=failed", reference="test:suite", scope=ObservationScope.TEST)
    second = _obs(c, "status=passed", reference="test:suite", scope=ObservationScope.TEST)
    second.supersedes = first.id
    snapshot = SnapshotBuilder().build([first, second], "Does the suite pass?", propositions=["The suite passes"])
    assert first.id not in snapshot.observation_ids
    assert [(o.observation_id, "superseded" in o.reason) for o in snapshot.omitted] == [(first.id, True)]
    # The anchor is told what was left out and why: blindness is never manufactured silently.
    assert render_packet(snapshot, [first, second])["omitted"][0]["observation_id"] == first.id


# -- 4. a material disagreement creates a durable, dependency-blocking hold -------------------


def _refuting_setup():
    c = _collector()
    observations = [_obs(c, "output.csv is absent", reference="artifact/output.csv", scope=ObservationScope.ARTIFACT)]
    snapshot = SnapshotBuilder().build(observations, "Was the output produced?", propositions=["The output was produced"])
    belief = _belief("The pipeline finished and produced its output", ["The output was produced"])
    assessment = RealityAnchor(
        lambda _p: {"verdict": "refuted", "refuted": ["The output was produced"], "evidence_refs": [observations[0].id]},
        _isolated(),
    ).assess(snapshot, observations, belief)
    return observations, snapshot, belief, assessment


def test_a_material_disagreement_holds_the_branch_and_its_dependencies():
    observations, snapshot, belief, assessment = _refuting_setup()
    disagreements = compare(belief, assessment, snapshot, observations, dependencies=["task-publish"])
    assert [d.kind for d in disagreements] == [DisagreementKind.CONTRADICTED]
    hold = open_hold(disagreements, branch="mission", frozen_revision=7)
    assert hold is not None and hold.status == HoldStatus.OPEN

    allowed, why = dispatch_allowed([hold], "task-publish")
    assert allowed is False and "held" in why
    # Unrelated authorized work continues.
    assert dispatch_allowed([hold], "task-unrelated")[0] is True
    # Narrowly scoped read-only evidence gathering stays open.
    assert dispatch_allowed([hold], "task-publish", tool="read_file")[0] is True


def test_a_hold_blocks_a_task_that_was_already_queued_and_survives_a_reload():
    state = MissionState(objective="Publish the report")
    queued = Task(title="Publish", status=TaskStatus.READY)
    state.tasks.append(queued)
    assert [t.id for t in Planner(state).compute_ready()] == [queued.id]

    observations, snapshot, belief, assessment = _refuting_setup()
    hold = open_hold(compare(belief, assessment, snapshot, observations, dependencies=[queued.id]), "mission", state.version)
    assert hold is not None
    state.holds.append(hold)

    assert Planner(state).compute_ready() == [], "an action queued before the hold is still held"
    assert queued.id in Planner(state).held_tasks()

    # The hold is durable state, so it survives a round-trip through persistence.
    reloaded = MissionState.model_validate_json(state.model_dump_json())
    assert reloaded.open_holds()[0].id == hold.id
    assert Planner(reloaded).compute_ready() == []


# -- 5. malformed, stale, fabricated and timed-out verdicts cannot pass a gate ----------------


def test_a_verdict_citing_an_observation_it_never_received_is_rejected():
    c = _collector()
    observations = [_obs(c, "value=5", reference="sensor/a")]
    snapshot = SnapshotBuilder().build(observations, "What is the value?", propositions=["The value is 5"])
    belief = _belief("The value is 5", ["The value is 5"])
    assessment = RealityAnchor(
        lambda _p: {"verdict": "supported", "supported": ["The value is 5"], "evidence_refs": ["obs_fabricated"]},
        _isolated(),
    ).assess(snapshot, observations, belief)

    problems = validate_assessment(assessment, snapshot, observations)
    assert any("not in its snapshot" in p for p in problems)
    assert compare(belief, assessment, snapshot, observations)[0].kind == DisagreementKind.MALFORMED_VERDICT


def test_an_unsealed_or_edited_verdict_is_rejected():
    c = _collector()
    observations = [_obs(c, "value=5", reference="sensor/a")]
    snapshot = SnapshotBuilder().build(observations, "What is the value?", propositions=["The value is 5"])
    belief = _belief("The value is 5", ["The value is 5"])

    unsealed = AnchorAssessment(snapshot_id=snapshot.id, question=snapshot.question, verdict=AnchorVerdict.SUPPORTED, supported=["The value is 5"])
    unsealed.isolation = _isolated()
    unsealed.isolation.evidence_manifest_hash = snapshot.content_hash
    assert any("never sealed" in p for p in validate_assessment(unsealed, snapshot, observations))

    sealed = RealityAnchor(lambda _p: {"verdict": "inconclusive"}, _isolated()).assess(snapshot, observations, belief)
    sealed.verdict = AnchorVerdict.SUPPORTED  # the executive cannot edit a sealed result
    sealed.supported = ["The value is 5"]
    assert sealed.tampered() is True
    assert compare(belief, sealed, snapshot, observations)[0].kind == DisagreementKind.MALFORMED_VERDICT


def test_an_observation_mutated_after_capture_invalidates_the_verdict():
    c = _collector()
    observations = [_obs(c, "value=5", reference="sensor/a")]
    snapshot = SnapshotBuilder().build(observations, "What is the value?", propositions=["The value is 5"])
    belief = _belief("The value is 5", ["The value is 5"])
    assessment = RealityAnchor(
        lambda _p: {"verdict": "supported", "supported": ["The value is 5"], "evidence_refs": [observations[0].id]},
        _isolated(),
    ).assess(snapshot, observations, belief)
    assert compare(belief, assessment, snapshot, observations) == []

    observations[0].content = "value=9"  # tampered after capture; the hash no longer matches
    assert any("changed after capture" in p for p in validate_assessment(assessment, snapshot, observations))


def test_a_timed_out_anchor_holds_rather_than_passes():
    c = _collector()
    observations = [_obs(c, "value=5", reference="sensor/a")]
    snapshot = SnapshotBuilder().build(observations, "What is the value?", propositions=["The value is 5"])
    belief = _belief("The value is 5", ["The value is 5"])

    def slow(_packet):
        raise TimeoutError("anchor exceeded its deadline")

    assessment = RealityAnchor(slow, _isolated()).assess(snapshot, observations, belief)
    assert assessment.verdict == AnchorVerdict.INCONCLUSIVE and assessment.is_sealed()
    disagreements = compare(belief, assessment, snapshot, observations)
    assert disagreements[0].kind == DisagreementKind.TIMEOUT
    assert open_hold(disagreements, "mission", 1) is not None


def test_unverifiable_isolation_is_a_disagreement_not_a_pass():
    c = _collector()
    observations = [_obs(c, "value=5", reference="sensor/a")]
    snapshot = SnapshotBuilder().build(observations, "What is the value?", propositions=["The value is 5"])
    belief = _belief("The value is 5", ["The value is 5"])
    not_fresh = IsolationMetadata(model="m", adapter="test", fresh_session=False)
    assessment = RealityAnchor(
        lambda _p: {"verdict": "supported", "supported": ["The value is 5"], "evidence_refs": [observations[0].id]},
        not_fresh,
    ).assess(snapshot, observations, belief)
    assert compare(belief, assessment, snapshot, observations)[0].kind == DisagreementKind.UNVERIFIABLE_ISOLATION


# -- 6. only new discriminating evidence resolves a hold --------------------------------------


def test_repeating_the_same_answer_does_not_resolve_a_hold():
    observations, snapshot, belief, assessment = _refuting_setup()
    hold = open_hold(compare(belief, assessment, snapshot, observations), "mission", 1)
    assert hold is not None
    before = {o.id for o in observations}

    repeat = ResolutionReceipt(hold_id=hold.id, disputed_proposition=hold.cause, new_observation_ids=[o.id for o in observations])
    ok, why = resolve_hold(hold, repeat, observations_before=before)
    assert ok is False and "captured after the hold" in why
    assert hold.status == HoldStatus.OPEN


def test_new_discriminating_evidence_resolves_the_hold():
    observations, snapshot, belief, assessment = _refuting_setup()
    hold = open_hold(compare(belief, assessment, snapshot, observations), "mission", 1)
    assert hold is not None
    before = {o.id for o in observations}

    c = _collector()
    produced = _obs(c, "output.csv present, 1042 rows", reference="artifact/output.csv", scope=ObservationScope.ARTIFACT)
    receipt = ResolutionReceipt(hold_id=hold.id, disputed_proposition=hold.cause, new_observation_ids=[produced.id])
    ok, why = resolve_hold(hold, receipt, observations_before=before)
    assert ok is True and "1 new observation" in why
    assert hold.status == HoldStatus.RESOLVED and hold.resolution_receipt_id == receipt.id
    assert dispatch_allowed([hold], "task-publish")[0] is True


def test_a_reassessment_that_still_refutes_does_not_clear_the_hold():
    observations, snapshot, belief, assessment = _refuting_setup()
    hold = open_hold(compare(belief, assessment, snapshot, observations), "mission", 1)
    assert hold is not None
    c = _collector()
    new_obs = _obs(c, "output.csv still absent", reference="artifact/output.csv", scope=ObservationScope.ARTIFACT)
    receipt = ResolutionReceipt(hold_id=hold.id, disputed_proposition=hold.cause, new_observation_ids=[new_obs.id])
    ok, why = resolve_hold(hold, receipt, observations_before={o.id for o in observations}, reassessment=assessment)
    assert ok is False and "still refutes" in why
    assert hold.status == HoldStatus.OPEN


# -- 7. agreement cannot authorise what deterministic checks refuse ---------------------------


def test_two_agreeing_anchors_cannot_authorise_a_missing_artifact():
    """The anchor is additional evidence. The completion gate is not delegated to it."""
    from pathlib import Path
    import tempfile

    from cogos.schemas.mission import Artifact, SuccessCriterion
    from cogos.verification.engine import VerificationEngine, mission_completion_check

    with tempfile.TemporaryDirectory(prefix="cogos-r1-") as d:
        path = Path(d) / "report.txt"
        path.write_text("findings", encoding="utf-8")
        state = MissionState(objective="Deliver the report")
        artifact = Artifact(name="report", path=str(path), summary="report")
        state.artifacts.append(artifact)
        state.resources["required_artifacts"] = [artifact.id]
        criterion = SuccessCriterion(description="report", verification_method="artifact")
        state.success_criteria.append(criterion)
        engine = VerificationEngine(None, state)
        engine.verify_artifact(artifact)
        engine.verify_criterion(criterion)
        assert mission_completion_check(state).status.value == "passed"

        path.unlink()
        c = _collector()
        observations = [_obs(c, "the team confirms the report was delivered", reference="note/1")]
        snapshot = SnapshotBuilder().build(observations, "Was the report delivered?", propositions=["report"])
        belief = _belief("The report was delivered", ["report"])
        for _ in range(2):
            a = RealityAnchor(lambda _p: {"verdict": "supported", "supported": ["report"], "evidence_refs": [observations[0].id]}, _isolated()).assess(snapshot, observations, belief)
            assert compare(belief, a, snapshot, observations) == [], "the anchors agree with each other"
        # And it changes nothing: the file is gone.
        assert mission_completion_check(state).status.value == "failed"


# -- 8. bounded resolution ends honestly rather than spinning ---------------------------------


def test_resolution_rounds_are_bounded_and_end_unresolved_not_agreed():
    observations, snapshot, belief, assessment = _refuting_setup()
    hold = open_hold(compare(belief, assessment, snapshot, observations), "mission", 1)
    assert hold is not None
    before = {o.id for o in observations}
    for _ in range(MAX_RESOLUTION_ROUNDS + 2):
        stale = ResolutionReceipt(hold_id=hold.id, disputed_proposition=hold.cause, new_observation_ids=[o.id for o in observations])
        resolve_hold(hold, stale, observations_before=before)
    assert hold.rounds >= MAX_RESOLUTION_ROUNDS
    assert hold.status == HoldStatus.UNRESOLVED_EXHAUSTED
    assert hold.status != HoldStatus.RESOLVED, "running out of rounds is not agreement"


# -- 9. isolation is described honestly, including what it does not establish -------------------


def test_isolation_metadata_records_its_own_limits():
    from cogos.evaluation.support import Sandbox

    sb = Sandbox("r1-isolation")
    try:
        meta = sb.runtime.executive.anchors._isolation(MissionState(objective="x"))
        assert meta.adapter == "scripted"
        assert any("protocol behaviour" in lim for lim in meta.known_limitations)
        assert meta.verifiable() is False, "a scripted anchor has no manifest bound until it assesses"
    finally:
        sb.cleanup()


def test_a_hold_is_reported_rather_than_becoming_a_completion():
    """End to end: a refuting anchor stops the mission completing, and says why."""
    from cogos.evaluation.support import Sandbox

    sb = Sandbox("r1-e2e", with_demo_project=True)
    from cogos.evaluation.scenarios import engineer_policy

    sb.adapter.policies["specialist"] = engineer_policy(sb.root)
    sb.adapter.policies["anchor"] = lambda req: {
        "verdict": "refuted",
        "refuted": ["The feature is implemented as described in the requirements", "The full test suite passes"],
        "evidence_refs": [],
        "missing_information": ["the observations do not show the requirement being met"],
        "uncertainty": 0.1,
    }
    try:
        state = sb.runtime.new_mission("Build the feature described in REQUIREMENTS.md.", context=sb.context())
        state = sb.runtime.run(state.mission_id, max_cycles=30)
        assert state.status.value != "complete", "a refuting anchor must stop completion"
        assert state.holds, "the disagreement is recorded as a durable hold"
        assert any(h.kind == DisagreementKind.CONTRADICTED for h in state.holds)
        assert any("reality anchor" in n for n in state.notes), "the reason is reported, not silent"
    finally:
        sb.cleanup()
