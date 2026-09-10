"""Verification records.

These live in `schemas` rather than in the engine because they are durable
mission state: `MissionState.verifications` holds them, so every gate that
claims to "require a verification record" can resolve the id and check the
outcome instead of testing that a list of ids is non-empty.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from cogos.ids import iso_now, new_id
from cogos.schemas.common import VerificationStatus


class VerificationCheck(BaseModel):
    name: str
    status: VerificationStatus
    detail: str = ""


class VerificationResult(BaseModel):
    id: str = Field(default_factory=lambda: new_id("ver"))
    target_type: str = Field(description="task|criterion|claim|artifact|code|data|decision|criteria|mission")
    target_id: str
    status: VerificationStatus
    summary: str
    checks: list[VerificationCheck] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    ran_at: str = Field(default_factory=iso_now)

    def failed_checks(self) -> list[VerificationCheck]:
        return [c for c in self.checks if c.status == VerificationStatus.FAILED]
