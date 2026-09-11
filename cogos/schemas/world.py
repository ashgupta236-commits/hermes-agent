"""World model schemas: entities, relations, causal links, temporal state."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from cogos.ids import iso_now, new_id
from cogos.schemas.common import EpistemicStatus, Provenance


class Property(BaseModel):
    name: str
    value: Any
    epistemic_status: EpistemicStatus = EpistemicStatus.OBSERVATION
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    provenance: Optional[Provenance] = None


class Entity(BaseModel):
    id: str = Field(default_factory=lambda: new_id("ent"))
    name: str
    kind: str = Field(default="thing", description="actor|organisation|resource|constraint|artifact|concept|thing")
    properties: list[Property] = Field(default_factory=list)
    incentives: list[str] = Field(default_factory=list)
    epistemic_status: EpistemicStatus = EpistemicStatus.OBSERVATION
    provenance: Optional[Provenance] = None
    updated_at: str = Field(default_factory=iso_now)


class Relation(BaseModel):
    id: str = Field(default_factory=lambda: new_id("rel"))
    source_id: str
    target_id: str
    kind: str
    epistemic_status: EpistemicStatus = EpistemicStatus.OBSERVATION
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    provenance: Optional[Provenance] = None


class CausalLink(BaseModel):
    id: str = Field(default_factory=lambda: new_id("cau"))
    cause: str
    effect: str
    mechanism: str = ""
    strength: float = Field(default=0.5, ge=0.0, le=1.0)
    epistemic_status: EpistemicStatus = EpistemicStatus.HYPOTHESIS
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence_ids: list[str] = Field(default_factory=list)


class Intervention(BaseModel):
    id: str = Field(default_factory=lambda: new_id("int"))
    description: str
    targets: list[str] = Field(default_factory=list)
    expected_effects: list[str] = Field(default_factory=list)
    reversibility: str = Field(default="reversible", description="reversible|partially_reversible|irreversible")


class Prediction(BaseModel):
    id: str = Field(default_factory=lambda: new_id("prd"))
    statement: str
    horizon: str = ""
    probability: float = Field(default=0.5, ge=0.0, le=1.0)
    based_on_claim_ids: list[str] = Field(default_factory=list)
    made_at: str = Field(default_factory=iso_now, description="Recorded before the outcome is known, which is what makes it a prediction")
    resolved: Optional[bool] = None
    resolved_at: Optional[str] = None
    outcome: str = ""
    #: The probability stated up front is never edited when the outcome arrives — a wrong
    #: prediction is evidence about the model, and rewriting it would destroy that evidence.
    stated_probability: Optional[float] = None


class WorldModel(BaseModel):
    entities: list[Entity] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    causal_links: list[CausalLink] = Field(default_factory=list)
    interventions: list[Intervention] = Field(default_factory=list)
    predictions: list[Prediction] = Field(default_factory=list)
    external_dependencies: list[str] = Field(default_factory=list)
    temporal_now: str = Field(default_factory=iso_now)
    history: list[dict[str, Any]] = Field(default_factory=list, description="Bounded log of state changes")

    def entity_by_name(self, name: str) -> Optional[Entity]:
        low = name.strip().lower()
        for e in self.entities:
            if e.name.strip().lower() == low:
                return e
        return None
