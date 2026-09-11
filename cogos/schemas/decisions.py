"""Decision journal schema."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from cogos.ids import iso_now, new_id


class Decision(BaseModel):
    decision_id: str = Field(default_factory=lambda: new_id("dec"))
    timestamp: str = Field(default_factory=iso_now)
    objective: str
    available_options: list[str] = Field(default_factory=list)
    selected_option: str
    concise_rationale: str
    decisive_evidence: list[str] = Field(default_factory=list, description="Evidence ids")
    assumptions: list[str] = Field(default_factory=list)
    expected_outcome: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    reversibility: str = Field(default="reversible", description="reversible|partially_reversible|irreversible")
    review_trigger: str = Field(default="", description="Condition under which this decision must be revisited")
    actual_outcome: Optional[str] = None
    outcome_success: Optional[bool] = None
    consequential: bool = Field(default=False, description="Whether the decision materially affects the mission result")
    domain: str = Field(default="general")
