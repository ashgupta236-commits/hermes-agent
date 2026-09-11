"""Four-channel decision evidence (R2).

For every material decision the runtime records four *linked* channels:

1. **declared** — what the executive said it would do and why;
2. **attempted** — what actually ran: model calls with their resolved identity and every billed
   attempt, tool calls at the real execution boundary, effective permissions, timing, usage;
3. **environment** — what the environment independently showed afterwards: receipts, test
   results, observation ids and artifact hashes;
4. **anchor** — what the blind assessment concluded about the same material.

The point of keeping them separate is that they disagree. A log line saying "command succeeded"
with no process result behind it is a *declaration*, not an environmental observation, and the
schema will not let it be filed as one. Where a channel has no data that is an explicit unknown,
never an implied success.

Private chain of thought is deliberately not a channel: it is not demanded, not reconstructed,
and would not be ground truth if it were.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from cogos.ids import iso_now, new_id


class ChannelName(str, Enum):
    DECLARED = "declared"
    ATTEMPTED = "attempted"
    ENVIRONMENT = "environment"
    ANCHOR = "anchor"


class DiscrepancyKind(str, Enum):
    CLAIMED_BUT_UNOBSERVED = "claimed_but_unobserved"
    UNEXPECTED_EFFECT = "unexpected_effect"
    EVIDENCE_REVISED = "evidence_revised"
    IDENTITY_MISMATCH = "identity_mismatch"
    ANCHOR_CONFLICT = "anchor_conflict"
    MISSING_TELEMETRY = "missing_telemetry"


class DeclaredChannel(BaseModel):
    operation: str = ""
    rationale: str = ""
    task_id: Optional[str] = None
    expected_checks: list[str] = Field(default_factory=list, description="What the executive said would be checked")
    consequential: bool = False


class ModelAttempt(BaseModel):
    kind: str = ""
    model_requested: str = ""
    models_used: list[str] = Field(default_factory=list)
    residency_status: str = "unknown"
    attempts: int = 1
    ok: bool = False
    error_kind: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0


class ToolAttempt(BaseModel):
    tool: str
    ok: bool = False
    error_kind: str = ""
    action_class: str = ""
    verdict: str = ""
    observed_effect: str = Field(default="", description="What the execution boundary actually reported")
    receipt_id: Optional[str] = None


class AttemptedChannel(BaseModel):
    model_calls: list[ModelAttempt] = Field(default_factory=list)
    tool_calls: list[ToolAttempt] = Field(default_factory=list)
    effective_permissions: list[str] = Field(default_factory=list)
    started_at: str = ""
    ended_at: str = ""

    def telemetry_present(self) -> bool:
        return bool(self.model_calls or self.tool_calls)


class EnvironmentChannel(BaseModel):
    observation_ids: list[str] = Field(default_factory=list)
    artifact_hashes: dict[str, str] = Field(default_factory=dict)
    test_records: list[str] = Field(default_factory=list)
    verification_ids: list[str] = Field(default_factory=list)

    def present(self) -> bool:
        return bool(self.observation_ids or self.artifact_hashes or self.test_records or self.verification_ids)


class AnchorChannel(BaseModel):
    assessment_id: Optional[str] = None
    verdict: str = ""
    hold_id: Optional[str] = None
    disagreement_kinds: list[str] = Field(default_factory=list)
    max_materiality: float = Field(default=0.0, ge=0.0, le=1.0, description="How much the difference bears on the decision")


class Discrepancy(BaseModel):
    id: str = Field(default_factory=lambda: new_id("dsc"))
    kind: DiscrepancyKind
    detail: str
    severity: float = Field(default=0.8, ge=0.0, le=1.0)
    channels: list[ChannelName] = Field(default_factory=list)
    raised_at: str = Field(default_factory=iso_now)


class DecisionRecord(BaseModel):
    """One material decision, with its four channels and every correlation id."""

    id: str = Field(default_factory=lambda: new_id("dec"))
    run_id: str = ""
    mission_id: str = ""
    branch: str = "mission"
    cycle: int = 0
    state_revision_start: int = 0
    state_revision_end: int = 0
    declared: DeclaredChannel = Field(default_factory=DeclaredChannel)
    attempted: AttemptedChannel = Field(default_factory=AttemptedChannel)
    environment: EnvironmentChannel = Field(default_factory=EnvironmentChannel)
    anchor: AnchorChannel = Field(default_factory=AnchorChannel)
    discrepancies: list[Discrepancy] = Field(default_factory=list)
    unknown_fields: list[str] = Field(default_factory=list, description="Channels or fields with no data: an explicit unknown, not a silent zero")
    created_at: str = Field(default_factory=iso_now)

    def correlation(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "mission_id": self.mission_id,
            "branch": self.branch,
            "decision_id": self.id,
            "cycle": self.cycle,
            "state_revision": [self.state_revision_start, self.state_revision_end],
            "observation_ids": self.environment.observation_ids,
            "verification_ids": self.environment.verification_ids,
            "anchor_assessment_id": self.anchor.assessment_id,
        }
