"""Binds the reality anchor (R1) to the running mission.

This is the seam between the persistent executive and the blind assessment. Everything the
anchor sees comes from the observation store; everything the executive believes is frozen into a
:class:`BeliefSnapshot` *before* the anchor runs; and the comparison that decides whether a hold
opens is deterministic kernel code in :mod:`cogos.verification.reality_anchor`.

The executive can trigger an anchor and can explain a disagreement afterwards. It cannot see the
packet before it goes out, edit a sealed verdict, or clear its own hold.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from cogos.adapters.base import CognitionRequest, ExecutiveUnavailable
from cogos.adapters.schema_utils import schema_for
from cogos.observability import ResourceLedger
from cogos.prompts import PROMPTS
from cogos.schemas.anchor import (
    AnchorAssessment,
    BeliefSnapshot,
    BranchHold,
    EvidenceSnapshot,
    IsolationMetadata,
    Observation,
    ObservationScope,
    ResolutionReceipt,
    stable_hash,
)
from cogos.schemas.cognition import AnchorVerdictSpec
from cogos.schemas.common import TrustLevel
from cogos.schemas.mission import MissionState
from cogos.verification.reality_anchor import (
    MAX_RESOLUTION_ROUNDS,
    ObservationCollector,
    RealityAnchor,
    SnapshotBuilder,
    compare,
    open_hold,
    resolve_hold,
    should_anchor,
)


class AnchorOutcome:
    def __init__(self, snapshot: EvidenceSnapshot, belief: BeliefSnapshot, assessment: AnchorAssessment, disagreements: list[Any], hold: Optional[BranchHold]):
        self.snapshot = snapshot
        self.belief = belief
        self.assessment = assessment
        self.disagreements = disagreements
        self.hold = hold

    @property
    def held(self) -> bool:
        return self.hold is not None

    def summary(self) -> str:
        if self.hold is None:
            return f"anchor agreed ({self.assessment.verdict.value}, uncertainty {self.assessment.uncertainty:.2f})"
        return f"anchor hold ({self.hold.kind.value}): {self.hold.cause[:200]}"


class AnchorService:
    def __init__(self, executive: Any):
        self.executive = executive
        self.config = executive.config
        self.tracer = executive.tracer

    # -- observation capture ------------------------------------------------------------

    def collect(self, state: MissionState) -> list[Observation]:
        """Capture the mission's current raw material. Idempotent per content: unchanged
        material supersedes nothing and is not duplicated."""
        collector = ObservationCollector(mission_id=state.mission_id, environment_id=str(self.config.repo_root))
        existing = {(o.reference, o.content_hash) for o in state.observations}
        latest_by_ref: dict[str, Observation] = {}
        for o in state.observations:
            latest_by_ref[o.reference] = o
        fresh: list[Observation] = []

        for artifact in state.artifacts:
            if not artifact.path:
                continue
            obs = collector.capture_file(Path(artifact.path), scope=ObservationScope.ARTIFACT)
            # What the ledger is entitled to say about this file, carried with the bytes.
            obs.authority = artifact.verified_scope or ""
            if (obs.reference, obs.content_hash) in existing:
                continue
            prior = latest_by_ref.get(obs.reference)
            obs.supersedes = prior.id if prior is not None else None
            latest_by_ref[obs.reference] = obs
            fresh.append(obs)

        for rec in state.tests[-10:]:
            # Keyed by command, so a later run of the same command supersedes the earlier one.
            # A fixed failure is a prior state, not a standing contradiction — and the
            # supersession is recorded, so the snapshot's omission manifest still shows the
            # anchor that an earlier run of this command existed and why it is not in the window.
            ref = f"test:{rec.name}"
            obs = collector.capture(
                f"command={rec.command}\nstatus={rec.status.value}\nsummary={rec.summary}\nran_at={rec.ran_at}\nreproduction={rec.expected_failure}",
                source="test ledger",
                reference=ref,
                scope=ObservationScope.TEST,
                trust=TrustLevel.VERIFIED_TOOL,
                authority=rec.authority or "",
            )
            if (obs.reference, obs.content_hash) in existing:
                continue
            prior = latest_by_ref.get(ref)
            obs.supersedes = prior.id if prior is not None else None
            latest_by_ref[ref] = obs
            fresh.append(obs)

        for ev in state.evidence[-20:]:
            ref = f"evidence:{ev.id}"
            src = ev.provenance.source or "evidence"
            obs = collector.capture(
                f"{ev.summary}\nsource={src}\nkind={getattr(ev.kind, 'value', ev.kind)}\n"
                f"supports={ev.supports_proposition}\nscope={ev.scope}\nfreshness={ev.freshness}",
                source=src,
                reference=ref,
                scope=ObservationScope.EXTERNAL_SOURCE,
                trust=TrustLevel.UNTRUSTED_EXTERNAL,
            )
            if (obs.reference, obs.content_hash) in existing:
                continue
            fresh.append(obs)

        state.observations.extend(fresh)
        return fresh

    # -- belief freeze --------------------------------------------------------------------

    def freeze_belief(self, state: MissionState, proposition: str, branch: str, validity_conditions: Optional[list[str]] = None) -> BeliefSnapshot:
        belief = BeliefSnapshot(
            branch=branch,
            proposition=proposition,
            support_ids=[c.id for c in state.claims if c.status.value in ("supported", "established")][:20],
            refutation_ids=[c.id for c in state.contradictions if not c.resolved][:20],
            uncertainty=round(1.0 - float(state.synthesis.get("confidence", 0.5) or 0.5), 4),
            validity_conditions=list(validity_conditions or []),
            executive_revision=state.version,
        )
        state.belief_snapshots.append(belief)
        return belief

    # -- the anchor call ------------------------------------------------------------------

    def _runner(self, state: MissionState):
        """A fresh, tool-less cognition call carrying only the packet.

        The headless adapter runs every call with `--no-session-persistence`, so there is no
        conversation to inherit; the executive's mission memory is never in the request because
        the request body is the rendered packet and nothing else. Inherited *project* context
        (CLAUDE.md, skills) is a real residual limitation and is recorded as one rather than
        claimed away.
        """

        def run(packet: dict[str, Any]) -> dict[str, Any]:
            req = CognitionRequest(
                kind="anchor",
                system_prompt=PROMPTS["anchor"],
                prompt="EVIDENCE PACKET:\n" + json.dumps(packet, default=str, indent=1)[:60000],
                schema_name="AnchorVerdictSpec",
                output_schema=schema_for(AnchorVerdictSpec),
                model=self.config.executive.model,
                mission_id=state.mission_id,
                tools=[],
                metadata={"anchor": True, "snapshot_id": packet.get("snapshot_id")},
            )
            try:
                resp = self.executive.adapter.call(req)
            except ExecutiveUnavailable as exc:
                raise TimeoutError(str(exc)) from exc
            # Accounting is shared with the mission: the same ResourceUsage object.
            ResourceLedger(state.usage).add_model_call(resp)
            if not resp.ok:
                raise RuntimeError(resp.error[:300] or "anchor call failed")
            return dict(resp.parsed or {})

        return run

    def _isolation(self, state: MissionState) -> IsolationMetadata:
        adapter = self.executive.adapter
        name = getattr(adapter, "name", type(adapter).__name__)
        limitations: list[str] = []
        fresh = True
        if name == "claude_code":
            limitations.append(
                "a fresh headless process may still load project context (CLAUDE.md, skills, MCP config); "
                "context independence is not statistical independence and the anchor shares the executive's model family"
            )
        elif name == "scripted":
            limitations.append("scripted anchor: establishes protocol behaviour only, not any reduction in model bias")
        else:
            limitations.append(f"isolation boundary for adapter '{name}' has not been independently checked")
        return IsolationMetadata(
            model=self.config.executive.model,
            adapter=name,
            fresh_session=fresh,
            tool_access=[],
            known_limitations=limitations,
        )

    # -- the protocol ----------------------------------------------------------------------

    def run(
        self,
        state: MissionState,
        *,
        question: str,
        proposition: str,
        branch: str = "mission",
        dependencies: Optional[list[str]] = None,
        definitions: Optional[list[str]] = None,
        propositions: Optional[list[str]] = None,
    ) -> AnchorOutcome:
        """Run the two-stage protocol.

        `propositions` are what the executive asserts, restated as the neutral question the
        anchor is asked. `proposition` is the executive's own headline conclusion, which the
        anchor never sees.
        """
        self.collect(state)
        props = list(propositions or [])
        belief = self.freeze_belief(state, proposition, branch, props)
        snapshot = SnapshotBuilder().build(
            state.observations,
            question,
            propositions=props,
            definitions=list(definitions or []) + _default_definitions(state),
            policy_revision=str(state.schema_version),
            mission_revision=state.version,
            missing=_known_missing(state),
        )
        state.evidence_snapshots.append(snapshot)

        anchor = RealityAnchor(self._runner(state), isolation=self._isolation(state))
        assessment = anchor.assess(snapshot, state.observations, belief)
        state.anchor_assessments.append(assessment)

        deps = list(dependencies or [t.id for t in state.tasks if t.status.value in ("pending", "ready")])
        verified = {
            c.description
            for c in state.success_criteria
            if c.satisfied and state.passing_verifications(c.verification_ids, target_type="criterion", target_id=c.id)
        }
        disagreements = compare(belief, assessment, snapshot, state.observations, dependencies=deps, independently_verified=verified)
        state.disagreements.extend(disagreements)
        hold = open_hold(disagreements, branch, state.version)
        if hold is not None:
            state.holds.append(hold)
        uncorroborated = [d.description for d in disagreements if d.materiality < 0.5]
        if uncorroborated:
            # Not a blocker, but not silence either: the final report has to say which
            # conclusions rest on the runtime's own checks with no second reading behind them.
            limits = state.resources.setdefault("anchor", {}).setdefault("uncorroborated", [])
            for item in uncorroborated:
                if item not in limits:
                    limits.append(item)
        state.resources.setdefault("anchor", {})["last_cycle"] = state.usage.cycles
        state.resources["anchor"]["last_observation_count"] = len(state.observations)
        self.tracer.emit(
            "anchor",
            f"{'HOLD' if hold else 'clear'} — {assessment.verdict.value} on '{question[:100]}'",
            data={
                "snapshot_id": snapshot.id,
                "assessment_id": assessment.id,
                "verdict": assessment.verdict.value,
                "disagreements": [d.kind.value for d in disagreements],
                "hold_id": hold.id if hold else None,
                "isolation": assessment.isolation.model_dump(mode="json"),
            },
        )
        return AnchorOutcome(snapshot, belief, assessment, disagreements, hold)

    # -- resolution -------------------------------------------------------------------------

    def attempt_resolution(self, state: MissionState, hold: BranchHold) -> tuple[bool, str]:
        """One bounded resolution round: capture new evidence, then re-assess against it.

        The receipt names only observations captured *after* the hold. If nothing new was found,
        the round is spent; after :data:`MAX_RESOLUTION_ROUNDS` the hold is recorded unresolved
        rather than argued away.
        """
        before = {o.id for o in state.observations}
        fresh = self.collect(state)
        receipt = ResolutionReceipt(
            hold_id=hold.id,
            disputed_proposition=hold.cause,
            new_observation_ids=[o.id for o in fresh],
            adjudication=f"round {hold.rounds + 1} of {MAX_RESOLUTION_ROUNDS}",
        )
        reassessment = None
        if fresh:
            snapshot = SnapshotBuilder().build(
                state.observations,
                f"Given the new evidence, does this still hold: {hold.cause[:200]}",
                definitions=_default_definitions(state),
                mission_revision=state.version,
                missing=_known_missing(state),
            )
            state.evidence_snapshots.append(snapshot)
            reassessment = RealityAnchor(self._runner(state), isolation=self._isolation(state)).assess(snapshot, state.observations, None)
            state.anchor_assessments.append(reassessment)
            receipt.anchor_assessment_id = reassessment.id
        state.resolution_receipts.append(receipt)
        ok, why = resolve_hold(hold, receipt, observations_before=before, reassessment=reassessment)
        self.tracer.emit("anchor", f"hold {hold.id} resolution: {'resolved' if ok else 'not resolved'} — {why}", data={"hold_id": hold.id, "rounds": hold.rounds, "status": hold.status.value})
        return ok, why

    def due(self, state: MissionState) -> bool:
        marker = state.resources.get("anchor", {})
        return should_anchor(
            state.usage.cycles - int(marker.get("last_cycle", 0) or 0),
            len(state.observations) - int(marker.get("last_observation_count", 0) or 0),
        )


def _default_definitions(state: MissionState) -> list[str]:
    """Interpretive context the anchor needs to read the observations at all.

    Withholding units, environment identity or timestamps would manufacture blindness rather
    than achieve it, so these always travel with the packet.
    """
    return [
        f"environment: {state.resources.get('root') or 'unspecified workspace'}",
        "test records: status 'passed' means the command exited 0; 'reproduction' marks a run that was expected to fail",
        "artifact observations carry the file's current bytes at capture time, with a content hash",
        "timestamps are ISO-8601 UTC",
    ]


def _known_missing(state: MissionState) -> list[str]:
    missing = [f"blocked operation: {b.operation} ({b.what_would_unblock})" for b in state.blocked_operations if not b.resolved]
    missing += [f"open unknown: {u.question}" for u in state.open_unknowns()[:5]]
    return missing[:10]
