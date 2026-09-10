"""Belief and evidence graph schemas."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from cogos.ids import iso_now, new_id
from cogos.schemas.common import EpistemicStatus, Provenance


class EvidenceKind(str, Enum):
    PRIMARY = "primary"  # direct observation, original document, test output
    SECONDARY = "secondary"  # reporting/analysis of a primary source
    TERTIARY = "tertiary"  # aggregations, encyclopedias, summaries


class Evidence(BaseModel):
    id: str = Field(default_factory=lambda: new_id("ev"))
    summary: str
    supports_claim_ids: list[str] = Field(default_factory=list)
    contradicts_claim_ids: list[str] = Field(default_factory=list)
    kind: EvidenceKind = EvidenceKind.SECONDARY
    provenance: Provenance
    content_excerpt: str = Field(default="", description="Bounded excerpt; full content lives in artifacts")
    supports_proposition: str = Field(
        default="", description="What this evidence *actually* establishes, which may be narrower than the claim"
    )
    scope: str = Field(default="", description="Definition/scope/period under which the evidence holds")
    freshness: Optional[str] = Field(default=None, description="Date the underlying fact was true/measured")
    independent_of: list[str] = Field(default_factory=list, description="Evidence ids known to be independent")
    derived_from: list[str] = Field(default_factory=list, description="Evidence ids this one repeats/derives from")
    weight: float = Field(default=0.5, ge=0.0, le=1.0)

    def root_sources(self) -> set[str]:
        """Lineage roots used to detect circular sourcing / false consensus."""
        return set(self.provenance.lineage) or {self.provenance.source}


class ClaimStatus(str, Enum):
    OPEN = "open"
    SUPPORTED = "supported"
    CONTESTED = "contested"
    REFUTED = "refuted"
    ESTABLISHED = "established"
    STALE = "stale"


class Claim(BaseModel):
    id: str = Field(default_factory=lambda: new_id("clm"))
    proposition: str
    status: ClaimStatus = ClaimStatus.OPEN
    epistemic_status: EpistemicStatus = EpistemicStatus.HYPOTHESIS
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence_for: list[str] = Field(default_factory=list)
    evidence_against: list[str] = Field(default_factory=list)
    source_quality: float = Field(default=0.5, ge=0.0, le=1.0)
    source_independence: int = Field(default=0, description="Count of independent root sources supporting")
    freshness: Optional[str] = None
    assumptions: list[str] = Field(default_factory=list)
    causal_dependencies: list[str] = Field(default_factory=list, description="Claim ids this depends on")
    predictions: list[str] = Field(default_factory=list)
    falsification_conditions: list[str] = Field(default_factory=list)
    decision_relevance: float = Field(default=0.5, ge=0.0, le=1.0, description="How much this claim can change the decision")
    last_verified_at: Optional[str] = None
    created_at: str = Field(default_factory=iso_now)
    updated_at: str = Field(default_factory=iso_now)
    provenance: Optional[Provenance] = None
    # --- temporal scope -------------------------------------------------------------
    # A claim about mutable state ("the file does not exist") is true *of a moment*, not
    # forever. When a later observation of the same subject disagrees, the earlier claim is
    # superseded rather than contradicted: both were accurate when made. History is kept —
    # the claim stays in state, queryable, pointing forward to what replaced it.
    observes_current_state: bool = Field(
        default=False,
        description="True when the proposition asserts the present state of something mutable, so a "
        "later observation of the same subject supersedes it instead of contradicting it",
    )
    subjects: list[str] = Field(
        default_factory=list,
        description="Normalised identifiers of what this claim asserts the current state of (e.g. file paths)",
    )
    observed_at: Optional[str] = Field(default=None, description="When the underlying state was actually observed")
    superseded_by: Optional[str] = Field(default=None, description="Claim id that replaced this one")
    superseded_at: Optional[str] = None

    def is_superseded(self) -> bool:
        return bool(self.superseded_by)

    def live(self) -> bool:
        """Whether this claim still describes the world as currently believed."""
        return not self.is_superseded() and self.status != ClaimStatus.STALE


class Hypothesis(BaseModel):
    """A candidate explanation competing in a tournament."""

    id: str = Field(default_factory=lambda: new_id("hyp"))
    question: str
    statement: str
    claim_id: Optional[str] = None
    explanatory_power: float = Field(default=0.5, ge=0.0, le=1.0)
    supporting_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)
    unique_predictions: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    disconfirming_observations: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    decision_consequences: str = ""
    status: str = Field(default="active", description="active|leading|eliminated|confirmed")
    prior: float = Field(default=0.5, ge=0.0, le=1.0)


class Contradiction(BaseModel):
    id: str = Field(default_factory=lambda: new_id("ctr"))
    claim_ids: list[str]
    evidence_ids: list[str] = Field(default_factory=list)
    description: str
    severity: float = Field(default=0.5, ge=0.0, le=1.0)
    suspected_cause: str = Field(
        default="unknown", description="scope|definition|time_period|source_error|measurement|genuine|unknown"
    )
    resolved: bool = False
    resolution: str = ""
    created_at: str = Field(default_factory=iso_now)
