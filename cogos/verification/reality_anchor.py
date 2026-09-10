"""Reality anchoring (R1): a blind second look at the same raw evidence.

The executive proposes; a collector captures raw observations; a *fresh* anchor reconstructs
the situation from those observations without seeing what the executive concluded; and
deterministic kernel code compares the two, applies holds, and gates dispatch.

Three properties are enforced here rather than asked for politely:

1. **The blind packet is built, not filtered.** :class:`SnapshotBuilder` assembles the packet
   from observations only. The executive's conclusion, synthesis, notes and progress narrative
   are not inputs to the builder at all, and :func:`assert_blind` re-checks the rendered packet
   against the frozen belief before the call goes out.
2. **The verdict is sealed before the position is revealed,** and validated afterwards: an
   assessment citing an observation that is not in its snapshot, or whose content no longer
   hashes to what was sealed, is a malformed verdict and creates a hold instead of passing one.
3. **A hold is cleared only by new evidence.** Repetition, higher confidence and agreement
   between two model instances are explicitly not resolutions.

A passing anchor is additional evidence. It is never an action grant, and it never replaces
task verification or the completion gate.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Optional

from cogos.ids import iso_now
from cogos.schemas.anchor import (
    AnchorAssessment,
    AnchorVerdict,
    BeliefSnapshot,
    BranchHold,
    DisagreementKind,
    EvidenceSnapshot,
    HoldStatus,
    IsolationMetadata,
    Observation,
    ObservationScope,
    OmittedItem,
    RealityDisagreement,
    ResolutionReceipt,
    stable_hash,
)
from cogos.schemas.common import TrustLevel

#: Automatic resolution rounds before an honest unresolved result. Deliberately small: an
#: unresolved dispute reported as unresolved beats an agreement manufactured by argument.
MAX_RESOLUTION_ROUNDS = 2

#: Development scheduling default from the brief. A testable choice, not an established optimum.
ANCHOR_EVERY_N_CYCLES = 5
ANCHOR_EVERY_N_OBSERVATIONS = 10

MAX_OBSERVATION_CHARS = 4000
MAX_PACKET_OBSERVATIONS = 40

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "the a an and or of to in on for with by from at as is are was were be been being that this "
    "these those it its not no but if so do does did has have had will would should could can".split()
)


def _content_words(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall((text or "").lower()) if len(w) >= 4 and w not in _STOP]


class BlindnessViolation(RuntimeError):
    """The packet about to be sent to the anchor carries the executive's own conclusion."""


# -- observation capture ------------------------------------------------------------------


class ObservationCollector:
    """Read-only capture of raw material, driven by the kernel rather than by the executive.

    The executive can ask for an observation to be *taken*; it cannot choose what the captured
    content says, edit it afterwards, or remove it from the store. Every observation is hashed
    at capture so a later mutation is detectable.
    """

    def __init__(self, mission_id: str = "", environment_id: str = "", collector: str = "kernel"):
        self.mission_id = mission_id
        self.environment_id = environment_id
        self.collector = collector

    def capture(
        self,
        content: str,
        *,
        source: str,
        reference: str = "",
        scope: ObservationScope = ObservationScope.TOOL_OUTPUT,
        trust: TrustLevel = TrustLevel.UNTRUSTED_EXTERNAL,
        units: str = "",
        supersedes: Optional[str] = None,
    ) -> Observation:
        text = (content or "")[:MAX_OBSERVATION_CHARS]
        obs = Observation(
            mission_id=self.mission_id,
            environment_id=self.environment_id,
            collector=self.collector,
            reference=reference or source,
            content=text,
            content_hash=stable_hash(text),
            source=source,
            scope=scope,
            trust=trust,
            units=units,
            supersedes=supersedes,
        )
        return obs

    def capture_file(self, path: Path, *, scope: ObservationScope = ObservationScope.ARTIFACT) -> Observation:
        p = Path(path)
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            missing = False
        except OSError as exc:
            text = f"<unreadable: {exc}>"
            missing = True
        obs = self.capture(text, source=str(p), reference=str(p), scope=scope, trust=TrustLevel.VERIFIED_TOOL if not missing else TrustLevel.SYSTEM)
        return obs

    def integrity_ok(self, obs: Observation) -> bool:
        return obs.content_hash == stable_hash(obs.content)


# -- snapshot -----------------------------------------------------------------------------


class SnapshotBuilder:
    """Builds the frozen evidence window the anchor will see.

    Selection is a stated rule applied to the observation store, and everything the rule leaves
    out is named in the omission manifest. That is what stops blindness from being manufactured:
    dropping the contradictory half is visible, because the drop itself is recorded.
    """

    def __init__(self, selection_rule: str = "all non-superseded observations for this mission, newest last"):
        self.selection_rule = selection_rule

    def build(
        self,
        observations: list[Observation],
        question: str,
        *,
        propositions: Optional[list[str]] = None,
        definitions: Optional[list[str]] = None,
        policy_revision: str = "",
        mission_revision: int = 0,
        missing: Optional[list[str]] = None,
    ) -> EvidenceSnapshot:
        superseded = {o.supersedes for o in observations if o.supersedes}
        kept: list[Observation] = []
        omitted: list[OmittedItem] = []
        for o in observations:
            if o.id in superseded:
                omitted.append(OmittedItem(observation_id=o.id, reason="superseded by a later observation"))
                continue
            kept.append(o)
        # A cap is a real omission and is recorded as one; contradictory material is never the
        # thing dropped, because the rule is positional, not content-based.
        if len(kept) > MAX_PACKET_OBSERVATIONS:
            for o in kept[:-MAX_PACKET_OBSERVATIONS]:
                omitted.append(OmittedItem(observation_id=o.id, reason=f"outside the {MAX_PACKET_OBSERVATIONS}-observation window"))
            kept = kept[-MAX_PACKET_OBSERVATIONS:]

        snapshot = EvidenceSnapshot(
            question=question,
            propositions=list(propositions or []),
            definitions=list(definitions or []),
            policy_revision=policy_revision,
            observation_ids=[o.id for o in kept],
            selection_rule=self.selection_rule,
            omitted=omitted,
            missing=list(missing or []),
            mission_revision=mission_revision,
        )
        snapshot.seal(observations)
        return snapshot


def render_packet(snapshot: EvidenceSnapshot, observations: list[Observation]) -> dict[str, Any]:
    """The exact material the anchor receives. Nothing the executive wrote appears in it."""
    by_id = {o.id: o for o in observations}
    return {
        "question": snapshot.question,
        "propositions_to_assess": snapshot.propositions,
        "definitions": snapshot.definitions,
        "policy_revision": snapshot.policy_revision,
        "selection_rule": snapshot.selection_rule,
        "observations": [by_id[oid].digest() for oid in snapshot.observation_ids if oid in by_id],
        "omitted": [{"observation_id": o.observation_id, "reason": o.reason} for o in snapshot.omitted],
        "known_missing": snapshot.missing,
        "snapshot_id": snapshot.id,
        "snapshot_hash": snapshot.content_hash,
    }


def assert_blind(packet: dict[str, Any], belief: Optional[BeliefSnapshot], *, overlap_threshold: float = 0.6) -> None:
    """Refuse to send a packet that carries the executive's committed position.

    Observations legitimately mention the same subject matter as the belief, so this is not a
    keyword ban: it fires when the belief's *distinctive wording* reappears close to verbatim,
    which is what a leaked conclusion looks like.
    """
    if belief is None:
        return
    # The propositions under assessment are legitimately in the packet — they are the question.
    # What must never be there is the executive's *answer* to them, or the narrative arguing for
    # it, so those are what this checks.
    proposition = belief.proposition.strip().lower()
    if not proposition:
        return
    neutral = {p.strip().lower() for p in packet.get("propositions_to_assess", [])}
    if proposition in neutral:
        return  # the committed position is being asked about, not asserted
    words = _content_words(belief.proposition)
    if len(words) < 4:
        return
    scrubbed = dict(packet)
    scrubbed.pop("propositions_to_assess", None)
    rendered = json.dumps(scrubbed, default=str).lower()
    if proposition in rendered:
        raise BlindnessViolation(f"the executive's conclusion appears verbatim in the anchor packet: {belief.proposition[:120]}")
    hits = sum(1 for w in set(words) if w in rendered)
    if hits / len(set(words)) >= overlap_threshold and belief.proposition[:60].lower() in rendered:
        raise BlindnessViolation("the executive's conclusion is restated in the anchor packet")


# -- the anchor ----------------------------------------------------------------------------

#: An anchor call takes the rendered packet and returns a raw verdict dict. Supplying this as a
#: callable is what lets a scripted anchor establish protocol behaviour in tests while a real
#: adapter supplies the isolation boundary in production.
AnchorRunner = Callable[[dict[str, Any]], dict[str, Any]]


class RealityAnchor:
    def __init__(self, runner: AnchorRunner, isolation: Optional[IsolationMetadata] = None):
        self.runner = runner
        self.isolation = isolation or IsolationMetadata()

    def assess(self, snapshot: EvidenceSnapshot, observations: list[Observation], belief: Optional[BeliefSnapshot] = None) -> AnchorAssessment:
        """Stage A. Build the packet, check it is blind, run the anchor, seal the verdict."""
        packet = render_packet(snapshot, observations)
        assert_blind(packet, belief)
        isolation = self.isolation.model_copy(deep=True)
        isolation.evidence_manifest_hash = snapshot.content_hash

        assessment = AnchorAssessment(snapshot_id=snapshot.id, question=snapshot.question, isolation=isolation)
        try:
            raw = self.runner(packet) or {}
        except TimeoutError as exc:
            assessment.verdict = AnchorVerdict.INCONCLUSIVE
            assessment.missing_information = [f"anchor timed out: {exc}"]
            assessment.isolation.known_limitations.append("verdict not produced: timeout")
            assessment.seal()
            return assessment
        except Exception as exc:  # noqa: BLE001 - a failed anchor is a hold, never a pass
            assessment.verdict = AnchorVerdict.INCONCLUSIVE
            assessment.missing_information = [f"anchor failed: {exc}"]
            assessment.isolation.known_limitations.append("verdict not produced: anchor error")
            assessment.seal()
            return assessment

        assessment.verdict = _coerce_verdict(raw.get("verdict"))
        assessment.supported = [str(s) for s in (raw.get("supported") or [])]
        assessment.refuted = [str(s) for s in (raw.get("refuted") or [])]
        assessment.unknown = [str(s) for s in (raw.get("unknown") or [])]
        assessment.evidence_refs = [str(s) for s in (raw.get("evidence_refs") or [])]
        assessment.alternatives = [str(s) for s in (raw.get("alternatives") or [])]
        assessment.missing_information = [str(s) for s in (raw.get("missing_information") or [])]
        try:
            assessment.uncertainty = min(1.0, max(0.0, float(raw.get("uncertainty", 0.5))))
        except (TypeError, ValueError):
            assessment.uncertainty = 1.0
            assessment.missing_information.append("anchor returned a malformed uncertainty")
        assessment.seal()
        return assessment


def _coerce_verdict(value: Any) -> AnchorVerdict:
    try:
        return AnchorVerdict(str(value))
    except ValueError:
        return AnchorVerdict.INCONCLUSIVE


# -- validation and comparison (Stage B) ----------------------------------------------------


def validate_assessment(assessment: AnchorAssessment, snapshot: EvidenceSnapshot, observations: list[Observation]) -> list[str]:
    """Structural problems that make a verdict unusable. Empty list means the verdict is readable."""
    problems: list[str] = []
    if not assessment.is_sealed():
        problems.append("verdict was never sealed")
    if assessment.tampered():
        problems.append("verdict no longer matches the hash it was sealed with")
    if assessment.snapshot_id != snapshot.id:
        problems.append(f"verdict is bound to snapshot {assessment.snapshot_id}, not {snapshot.id}")
    if assessment.isolation.evidence_manifest_hash != snapshot.content_hash:
        problems.append("verdict's evidence manifest hash does not match the snapshot it claims to assess")
    if snapshot.content_hash != snapshot.seal(observations):
        problems.append("the snapshot's own content no longer matches its manifest")
    manifest = set(snapshot.observation_ids)
    fabricated = [ref for ref in assessment.evidence_refs if ref not in manifest]
    if fabricated:
        problems.append("verdict cites observations that are not in its snapshot: " + ", ".join(sorted(fabricated)[:5]))
    by_id = {o.id: o for o in observations}
    mutated = [oid for oid in snapshot.observation_ids if oid in by_id and by_id[oid].content_hash != stable_hash(by_id[oid].content)]
    if mutated:
        problems.append("observations changed after capture: " + ", ".join(sorted(mutated)[:5]))
    if not assessment.isolation.verifiable():
        problems.append("isolation could not be verified (no fresh session or no bound manifest)")
    return problems


def compare(
    belief: BeliefSnapshot,
    assessment: AnchorAssessment,
    snapshot: EvidenceSnapshot,
    observations: list[Observation],
    *,
    dependencies: Optional[list[str]] = None,
    independently_verified: Optional[set[str]] = None,
) -> list[RealityDisagreement]:
    """Deterministic kernel comparison. Neither the executive nor the anchor runs this.

    `independently_verified` names propositions that already carry a deterministic passing
    receipt from the verification engine. It changes only one thing: how material it is that the
    anchor could not *settle* such a proposition. An anchor that cannot reach a conclusion the
    kernel independently verified is a limit on the second opinion, recorded as such; an anchor
    that **refutes** it is a material contradiction regardless, which is the whole point of
    running one.
    """
    deps = list(dependencies or [])
    problems = validate_assessment(assessment, snapshot, observations)
    out: list[RealityDisagreement] = []
    if problems:
        kind = DisagreementKind.UNVERIFIABLE_ISOLATION if all("isolation" in p for p in problems) else DisagreementKind.MALFORMED_VERDICT
        out.append(
            RealityDisagreement(
                belief_id=belief.id,
                anchor_id=assessment.id,
                kind=kind,
                materiality=1.0,
                description="; ".join(problems),
                affected_preconditions=list(belief.validity_conditions),
                dependencies=deps,
                required_check="re-run the anchor with a valid, sealed verdict bound to the current snapshot",
            )
        )
        return out

    # The executive asserts every proposition in the snapshot. The anchor answered each one from
    # the observations alone. Compare them one by one: a refutation is a contradiction, an
    # unsettled proposition is missing evidence, and neither is cured by confidence.
    asserted = list(snapshot.propositions) or [belief.proposition]
    for proposition in asserted:
        if _matches_any(proposition, assessment.refuted):
            out.append(
                RealityDisagreement(
                    belief_id=belief.id,
                    anchor_id=assessment.id,
                    kind=DisagreementKind.CONTRADICTED,
                    materiality=1.0,
                    description=f"blind assessment refutes a proposition the executive committed to: {proposition[:160]}",
                    affected_preconditions=[proposition],
                    dependencies=deps,
                    required_check="a discriminating observation that distinguishes the two readings of the same evidence",
                )
            )
            continue
        if _matches_any(proposition, assessment.supported):
            continue
        kind = DisagreementKind.TIMEOUT if any("timed out" in m for m in assessment.missing_information) else DisagreementKind.MISSING_EVIDENCE
        corroborated = proposition in (independently_verified or set())
        materiality = 0.3 if (corroborated and kind == DisagreementKind.MISSING_EVIDENCE) else 0.8
        out.append(
            RealityDisagreement(
                belief_id=belief.id,
                anchor_id=assessment.id,
                kind=kind,
                materiality=materiality,
                description=("the blind assessment could not independently corroborate a proposition the runtime verified deterministically: " if corroborated else "blind assessment could not establish from the observations: ")
                + f"{proposition[:160]}"
                + (" — " + "; ".join(assessment.missing_information[:2]) if assessment.missing_information else ""),
                affected_preconditions=[proposition],
                dependencies=deps,
                required_check=assessment.missing_information[0] if assessment.missing_information else "capture the decisive observation and re-run the anchor",
            )
        )
    if not out and not assessment.evidence_refs:
        out.append(
            RealityDisagreement(
                belief_id=belief.id,
                anchor_id=assessment.id,
                kind=DisagreementKind.MISSING_EVIDENCE,
                materiality=0.7,
                description="the blind assessment supports the position but cites no observation for it",
                dependencies=deps,
                required_check="cite the observations that support the proposition",
            )
        )
    return out


def _matches_any(proposition: str, candidates: list[str], threshold: float = 0.6) -> bool:
    """Same proposition, allowing for rewording. Not a substring test."""
    want = set(_content_words(proposition))
    if not want:
        return False
    for cand in candidates:
        got = set(_content_words(cand))
        if got and len(want & got) / len(want) >= threshold:
            return True
    return False


# -- holds ----------------------------------------------------------------------------------


def open_hold(disagreements: list[RealityDisagreement], branch: str, frozen_revision: int) -> Optional[BranchHold]:
    """Create a hold for material disagreements. Immaterial differences do not stop work."""
    material = [d for d in disagreements if d.materiality >= 0.5]
    if not material:
        return None
    worst = max(material, key=lambda d: d.materiality)
    deps: list[str] = []
    for d in material:
        for dep in d.dependencies:
            if dep not in deps:
                deps.append(dep)
    return BranchHold(
        branch=branch,
        dependencies=deps,
        cause=worst.description[:400],
        kind=worst.kind,
        disagreement_ids=[d.id for d in material],
        frozen_revision=frozen_revision,
    )


def dispatch_allowed(holds: list[BranchHold], branch_or_task_id: str, tool: Optional[str] = None) -> tuple[bool, str]:
    """Checked at dispatch, not at queue time: an action queued before a hold is still held."""
    for hold in holds:
        if not hold.blocks(branch_or_task_id):
            continue
        if tool and tool in hold.allowed_actions:
            continue  # narrowly scoped read-only evidence gathering stays open
        return False, f"branch '{branch_or_task_id}' is held ({hold.kind.value}): {hold.cause[:200]}"
    return True, ""


def resolve_hold(
    hold: BranchHold,
    receipt: ResolutionReceipt,
    *,
    observations_before: set[str],
    reassessment: Optional[AnchorAssessment] = None,
) -> tuple[bool, str]:
    """Clear a hold only on new discriminating evidence.

    Repetition is not resolution: a receipt whose observations all existed when the hold was
    created settles nothing, and neither does a second opinion with no new material behind it.
    """
    if hold.status != HoldStatus.OPEN:
        return False, f"hold is already {hold.status.value}"
    if receipt.hold_id != hold.id:
        return False, "receipt is for a different hold"
    new_ids = [oid for oid in receipt.new_observation_ids if oid not in observations_before]
    if not new_ids:
        hold.rounds += 1
        if hold.rounds >= MAX_RESOLUTION_ROUNDS:
            hold.status = HoldStatus.UNRESOLVED_EXHAUSTED
            hold.resolved_at = iso_now()
            return False, "no new discriminating evidence after the permitted resolution rounds; recorded unresolved"
        return False, "resolution requires evidence captured after the hold was created"
    if reassessment is not None and not reassessment.is_sealed():
        return False, "the re-assessment was never sealed"
    if reassessment is not None and reassessment.verdict == AnchorVerdict.REFUTED:
        hold.rounds += 1
        return False, "the re-assessment still refutes the disputed proposition"
    hold.status = HoldStatus.RESOLVED
    hold.resolution_receipt_id = receipt.id
    hold.resolved_at = iso_now()
    return True, f"resolved on {len(new_ids)} new observation(s)"


# -- deterministic anchor --------------------------------------------------------------------


def deterministic_anchor(packet: dict[str, Any]) -> dict[str, Any]:
    """Answer the packet's propositions from its observations, with no model involved.

    This is a real independent reading, not a stand-in that approves: it sees only the packet,
    it settles a proposition only when a passing test observation or an intact artifact
    observation bears on it, and it returns `inconclusive` naming what is missing otherwise. A
    failing test observation refutes the proposition it bears on.

    Its reach is narrow by construction — it can adjudicate mechanically checkable claims about
    artifacts and tests, and nothing else — so a mission whose propositions are not of that kind
    gets an honest `unknown` rather than a manufactured verdict.
    """
    observations = list(packet.get("observations") or [])
    propositions = [str(p) for p in (packet.get("propositions_to_assess") or []) if str(p).strip()]
    supported: list[str] = []
    refuted: list[str] = []
    unknown: list[str] = []
    refs: list[str] = []
    missing: list[str] = list(packet.get("known_missing") or [])

    failing: list[dict[str, Any]] = []
    passing: list[dict[str, Any]] = []
    intact_artifacts: list[dict[str, Any]] = []
    for o in observations:
        content = str(o.get("content") or "")
        if o.get("scope") == "test":
            if "status=failed" in content and "reproduction=True" not in content:
                failing.append(o)
            elif "status=passed" in content:
                passing.append(o)
        elif o.get("scope") == "artifact":
            if content.startswith("<unreadable:") or not content.strip():
                continue
            intact_artifacts.append(o)

    if not observations:
        return {
            "verdict": "inconclusive",
            "unknown": propositions,
            "missing_information": ["no observations were captured for this question"] + missing,
            "uncertainty": 1.0,
        }

    for proposition in propositions:
        bearing_fail = [o for o in failing if _bears_on(proposition, o)]
        if bearing_fail:
            refuted.append(proposition)
            refs.extend(str(o["id"]) for o in bearing_fail[:3])
            continue
        bearing = [o for o in passing + intact_artifacts if _bears_on(proposition, o)]
        if bearing:
            supported.append(proposition)
            refs.extend(str(o["id"]) for o in bearing[:3])
            continue
        unknown.append(proposition)

    if refuted:
        verdict = "refuted"
    elif supported and not unknown:
        verdict = "supported"
    else:
        verdict = "inconclusive"
    if unknown:
        missing.insert(0, "no observation bears on: " + "; ".join(u[:80] for u in unknown[:3]))
    return {
        "verdict": verdict,
        "supported": supported,
        "refuted": refuted,
        "unknown": unknown,
        "evidence_refs": sorted(set(refs)),
        "alternatives": [],
        "missing_information": missing[:6],
        "uncertainty": round(len(unknown) / max(1, len(propositions)), 3),
    }


_ARTIFACT_HINTS = ("artifact", "file", "report", "document", "deliverable", "output", "written", "produced", "implement")
_TEST_HINTS = ("test", "suite", "pytest", "passes", "passing", "exits", "green", "verified", "works", "correct", "implement")


def _bears_on(proposition: str, observation: dict[str, Any]) -> bool:
    """Does this observation speak to this proposition?

    Deliberately conservative: either the proposition's own words show up in the observation, or
    the proposition is of a kind (about tests, about artifacts) the observation's scope covers.
    Anything else is `unknown`, which is the honest answer for a deterministic reader.
    """
    words = set(_content_words(proposition))
    haystack = " ".join(str(observation.get(k) or "") for k in ("content", "source", "reference", "units")).lower()
    if words and len(words & set(_WORD_RE.findall(haystack))) / len(words) >= 0.3:
        return True
    low = proposition.lower()
    scope = observation.get("scope")
    if scope == "test" and any(h in low for h in _TEST_HINTS):
        return True
    if scope == "artifact" and any(h in low for h in _ARTIFACT_HINTS):
        return True
    return False


def should_anchor(cycles_since: int, observations_since: int) -> bool:
    """The scheduling default. Configurable, and explicitly not a research-established optimum."""
    return cycles_since >= ANCHOR_EVERY_N_CYCLES or observations_since >= ANCHOR_EVERY_N_OBSERVATIONS
