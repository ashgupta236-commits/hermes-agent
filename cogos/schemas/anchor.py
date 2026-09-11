"""Reality-anchoring records (R1).

A persistent executive is not a neutral judge of whether its own current interpretation still
fits the evidence: it has a proposal, a narrative and sunk cost. These records exist so a
*separate* assessment of the same raw material can be made, sealed, and compared by
deterministic kernel code that neither side can edit.

The relationships that matter here, and that the code enforces:

* an :class:`Observation` is raw material with a hash, captured by a collector the executive
  does not drive;
* an :class:`EvidenceSnapshot` is a frozen, manifest-ed window over observations, with its
  selection rule and everything it left out recorded, so blindness cannot be manufactured by
  quietly dropping the inconvenient half;
* an :class:`AnchorAssessment` is sealed before the executive's position is revealed, and may
  only cite observations that are actually in its snapshot;
* a :class:`BranchHold` is created by the kernel and cleared only by a resolution receipt tying
  *new* evidence to the disputed proposition.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from cogos.ids import iso_now, new_id
from cogos.schemas.common import TrustLevel


def stable_hash(payload: Any) -> str:
    """Deterministic sha256 over a JSON-serialisable payload."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


class ObservationScope(str, Enum):
    """What the observation is authoritative *about*. Scope mismatches are a disagreement type."""

    ENVIRONMENT = "environment"
    ARTIFACT = "artifact"
    TEST = "test"
    EXTERNAL_SOURCE = "external_source"
    TOOL_OUTPUT = "tool_output"
    DECLARED = "declared"


class Observation(BaseModel):
    id: str = Field(default_factory=lambda: new_id("obs"))
    mission_id: str = ""
    environment_id: str = Field(default="", description="Which environment this was observed in; a renamed workspace is a different environment")
    observed_at: str = Field(default_factory=iso_now)
    collector: str = Field(default="kernel", description="Who captured it. The executive is never the collector of its own anchor evidence")
    reference: str = Field(default="", description="Where the raw object lives (path, URL, tool call id)")
    content_hash: str = ""
    content: str = Field(default="", description="Raw captured content, truncated by the collector")
    source: str = ""
    scope: ObservationScope = ObservationScope.TOOL_OUTPUT
    trust: TrustLevel = TrustLevel.UNTRUSTED_EXTERNAL
    units: str = Field(default="", description="Units/definitions needed to read the value; omitting these manufactures false blindness")
    #: How much the observed material is allowed to prove — `EvidenceAuthority` for a test
    #: record, the artifact's `verified_scope` for a file. The blind assessment reads it: a
    #: self-reported pass and an attested one look identical in the summary text, and treating
    #: them alike let the second opinion corroborate a fabricated result.
    authority: str = ""
    supersedes: Optional[str] = Field(default=None, description="An earlier observation this replaces")

    def digest(self) -> dict[str, Any]:
        """What the anchor is allowed to see. No executive commentary passes through here."""
        return {
            "id": self.id,
            "observed_at": self.observed_at,
            "environment_id": self.environment_id,
            "collector": self.collector,
            "reference": self.reference,
            "content_hash": self.content_hash,
            "content": self.content,
            "source": self.source,
            "scope": self.scope.value,
            "trust": self.trust.value,
            "units": self.units,
            "supersedes": self.supersedes,
        }


class OmittedItem(BaseModel):
    observation_id: str
    reason: str


class EvidenceSnapshot(BaseModel):
    id: str = Field(default_factory=lambda: new_id("snp"))
    question: str
    definitions: list[str] = Field(default_factory=list)
    policy_revision: str = ""
    propositions: list[str] = Field(
        default_factory=list,
        description="The propositions to adjudicate, stated neutrally. These are the *question*, "
        "not the answer: the executive's verdict on them is withheld from the packet.",
    )
    observation_ids: list[str] = Field(default_factory=list, description="Ordered manifest")
    selection_rule: str = ""
    omitted: list[OmittedItem] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list, description="Material the collector knows it could not obtain")
    mission_revision: int = 0
    created_at: str = Field(default_factory=iso_now)
    content_hash: str = ""

    def seal(self, observations: list[Observation]) -> str:
        """Bind the snapshot to the exact observation content it names."""
        by_id = {o.id: o for o in observations}
        payload = {
            "question": self.question,
            "propositions": list(self.propositions),
            "definitions": sorted(self.definitions),
            "policy_revision": self.policy_revision,
            "manifest": [{"id": oid, "hash": by_id[oid].content_hash if oid in by_id else None} for oid in self.observation_ids],
            "omitted": sorted((o.observation_id, o.reason) for o in self.omitted),
            "missing": sorted(self.missing),
        }
        self.content_hash = stable_hash(payload)
        return self.content_hash


class BeliefSnapshot(BaseModel):
    """The executive's committed position, frozen before the anchor runs."""

    id: str = Field(default_factory=lambda: new_id("bsn"))
    branch: str = Field(default="mission", description="What this position governs: a mission, task or criterion id")
    proposition: str
    support_ids: list[str] = Field(default_factory=list)
    refutation_ids: list[str] = Field(default_factory=list)
    uncertainty: float = Field(default=0.5, ge=0.0, le=1.0)
    validity_conditions: list[str] = Field(default_factory=list)
    executive_revision: int = 0
    created_at: str = Field(default_factory=iso_now)


class IsolationMetadata(BaseModel):
    """What was actually done to keep the anchor blind, and what could not be guaranteed."""

    model: str = ""
    adapter: str = ""
    prompt_version: str = "anchor/1"
    session_id: Optional[str] = None
    fresh_session: bool = False
    tool_access: list[str] = Field(default_factory=list)
    evidence_manifest_hash: str = ""
    known_limitations: list[str] = Field(default_factory=list)

    def verifiable(self) -> bool:
        """Isolation is only claimed when a fresh session and a bound manifest are both present."""
        return bool(self.fresh_session and self.evidence_manifest_hash)


class AnchorVerdict(str, Enum):
    SUPPORTED = "supported"
    REFUTED = "refuted"
    INCONCLUSIVE = "inconclusive"


class AnchorAssessment(BaseModel):
    id: str = Field(default_factory=lambda: new_id("anc"))
    snapshot_id: str
    question: str
    verdict: AnchorVerdict = AnchorVerdict.INCONCLUSIVE
    supported: list[str] = Field(default_factory=list)
    refuted: list[str] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list, description="Observation ids; every one must be in the snapshot manifest")
    alternatives: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    uncertainty: float = Field(default=0.5, ge=0.0, le=1.0)
    isolation: IsolationMetadata = Field(default_factory=IsolationMetadata)
    created_at: str = Field(default_factory=iso_now)
    sealed_at: Optional[str] = None
    sealed_hash: str = ""

    def seal(self) -> str:
        """Freeze the verdict. Sealing happens before the executive's position is revealed."""
        payload = {
            "snapshot_id": self.snapshot_id,
            "question": self.question,
            "verdict": self.verdict.value,
            "supported": list(self.supported),
            "refuted": list(self.refuted),
            "unknown": list(self.unknown),
            "evidence_refs": sorted(self.evidence_refs),
            "missing_information": sorted(self.missing_information),
            "uncertainty": self.uncertainty,
        }
        self.sealed_hash = stable_hash(payload)
        self.sealed_at = iso_now()
        return self.sealed_hash

    def is_sealed(self) -> bool:
        return bool(self.sealed_at and self.sealed_hash)

    def tampered(self) -> bool:
        """True when the record no longer hashes to what was sealed."""
        if not self.is_sealed():
            return False
        before = (self.sealed_hash, self.sealed_at)
        current = stable_hash(
            {
                "snapshot_id": self.snapshot_id,
                "question": self.question,
                "verdict": self.verdict.value,
                "supported": list(self.supported),
                "refuted": list(self.refuted),
                "unknown": list(self.unknown),
                "evidence_refs": sorted(self.evidence_refs),
                "missing_information": sorted(self.missing_information),
                "uncertainty": self.uncertainty,
            }
        )
        self.sealed_hash, self.sealed_at = before
        return current != before[0]


class DisagreementKind(str, Enum):
    CONTRADICTED = "contradicted"
    UNSUPPORTED = "unsupported"
    SCOPE_MISMATCH = "scope_mismatch"
    MISSING_EVIDENCE = "missing_evidence"
    UNVERIFIABLE_ISOLATION = "unverifiable_isolation"
    MALFORMED_VERDICT = "malformed_verdict"
    TIMEOUT = "timeout"


class RealityDisagreement(BaseModel):
    id: str = Field(default_factory=lambda: new_id("dis"))
    belief_id: str
    anchor_id: str
    kind: DisagreementKind
    materiality: float = Field(default=1.0, ge=0.0, le=1.0, description="How much the difference bears on the decision at hand")
    description: str = ""
    affected_preconditions: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list, description="Task/branch ids that rest on the disputed proposition")
    required_check: str = Field(default="", description="The discriminating observation that would settle it")
    created_at: str = Field(default_factory=iso_now)


class HoldStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    UNRESOLVED_EXHAUSTED = "unresolved_exhausted"


class ResolutionReceipt(BaseModel):
    id: str = Field(default_factory=lambda: new_id("rcp"))
    hold_id: str
    disputed_proposition: str
    new_observation_ids: list[str] = Field(default_factory=list, description="Observations captured *after* the hold was created")
    anchor_assessment_id: Optional[str] = None
    adjudication: str = ""
    created_at: str = Field(default_factory=iso_now)


class BranchHold(BaseModel):
    id: str = Field(default_factory=lambda: new_id("hold"))
    branch: str
    dependencies: list[str] = Field(default_factory=list)
    cause: str = ""
    kind: DisagreementKind = DisagreementKind.CONTRADICTED
    disagreement_ids: list[str] = Field(default_factory=list)
    frozen_revision: int = 0
    created_by: str = "kernel"
    status: HoldStatus = HoldStatus.OPEN
    allowed_actions: list[str] = Field(
        default_factory=lambda: ["read_file", "list_dir", "search_text", "read_document", "memory_search"],
        description="Read-only evidence gathering permitted while the hold stands",
    )
    rounds: int = Field(default=0, description="Automatic resolution attempts already spent")
    resolution_receipt_id: Optional[str] = None
    created_at: str = Field(default_factory=iso_now)
    resolved_at: Optional[str] = None

    def blocks(self, branch_or_task_id: str) -> bool:
        return self.status == HoldStatus.OPEN and (branch_or_task_id == self.branch or branch_or_task_id in self.dependencies)
