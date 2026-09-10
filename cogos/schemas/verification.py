"""Verification records.

These live in `schemas` rather than in the engine because they are durable
mission state: `MissionState.verifications` holds them, so every gate that
claims to "require a verification record" can resolve the id and check the
outcome instead of testing that a list of ids is non-empty.
"""

from __future__ import annotations

from typing import Optional

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

    def binds_to(self, target_type: str, target_id: str) -> bool:
        """True when this record was actually produced against that exact target."""
        return self.target_type == target_type and self.target_id == target_id

    def is_evidence_for(self, target_type: str, target_id: str) -> bool:
        """A receipt is evidence only when it passed *and* binds to the thing it is cited for."""
        return self.status == VerificationStatus.PASSED and self.binds_to(target_type, target_id)


def cite(holder_ids: list[str], result: VerificationResult, target_id: str, target_type: Optional[str] = None) -> bool:
    """Record `result` as evidence on `holder_ids`, refusing an unbound or non-passing receipt.

    Binding is on the *target id*: that is what says "this check was run against this thing".
    The type label is a coarser description (a task may be verified as `code`, `research` or
    `task`), so it is only checked when the caller states one. Citation is where the invariant
    is cheapest to enforce, so validation happens at insertion rather than only at the gate.
    Returns True when the citation was accepted.
    """
    if result.status != VerificationStatus.PASSED or result.target_id != target_id:
        return False
    if target_type is not None and result.target_type != target_type:
        return False
    if result.id not in holder_ids:
        holder_ids.append(result.id)
    return True
