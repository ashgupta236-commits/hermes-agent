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
    authoritative: bool = Field(
        default=False,
        description="This check is a direct runtime observation — a tool result, a test-run "
        "classification, a file hash — rather than a gap the executive may reason about. "
        "Executive judgement may not upgrade a non-passing authoritative check: model reasoning "
        "is supplemental to execution evidence, never a substitute for it.",
    )


class InputVersion(BaseModel):
    """The exact version of one input a verification was produced against.

    A receipt that does not say *which* bytes it checked cannot distinguish proving version A
    from proving version B. The completion gate re-reads these before accepting the receipt.
    """

    path: str
    content_hash: str = ""
    artifact_id: Optional[str] = None
    observed_at: str = Field(default_factory=iso_now)
    changed_during_verification: bool = Field(
        default=False,
        description="The bytes moved between the pre-run snapshot and the post-run read, so this "
        "run never observed a single coherent version of the input.",
    )


class VerificationResult(BaseModel):
    id: str = Field(default_factory=lambda: new_id("ver"))
    target_type: str = Field(description="task|criterion|claim|artifact|code|data|decision|criteria|mission")
    target_id: str
    status: VerificationStatus
    summary: str
    checks: list[VerificationCheck] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    ran_at: str = Field(default_factory=iso_now)
    input_versions: list[InputVersion] = Field(
        default_factory=list,
        description="Relevant input versions this receipt was produced against. Empty on records "
        "written before version binding existed, which the gate treats as 'unknown', never as 'unchanged'.",
    )
    #: The highest evidence authority anything in this receipt reaches. Deserialises **down**:
    #: an absent value is `untrusted_self_report`, because a missing field is indistinguishable
    #: from a stripped one and from a receipt written under rules that admitted forged passes.
    authority: str = ""
    produced_by_task_id: Optional[str] = None
    produced_by_action_ids: list[str] = Field(
        default_factory=list,
        description="Tool call ids whose results this verification read, so an action can be "
        "traversed forward to every criterion that relied on it.",
    )

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
