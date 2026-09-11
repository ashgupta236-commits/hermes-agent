"""Tool fabric schemas."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from cogos.ids import iso_now, new_id
from cogos.schemas.common import ActionClass, PolicyDecision, TrustLevel


class ToolSpec(BaseModel):
    name: str
    description: str
    substrate: str = Field(description="filesystem|shell|git|python|tests|web|calc|memory|mcp|api|db|documents")
    default_action_class: ActionClass = ActionClass.REVERSIBLE_LOCAL
    parameters_schema: dict[str, Any] = Field(default_factory=dict)
    output_trust: TrustLevel = TrustLevel.VERIFIED_TOOL
    deterministic: bool = True
    network: bool = False
    available: bool = True
    unavailable_reason: str = ""


class ToolCall(BaseModel):
    id: str = Field(default_factory=lambda: new_id("call"))
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    task_id: Optional[str] = None
    purpose: str = ""
    requested_at: str = Field(default_factory=iso_now)


class FirewallVerdict(BaseModel):
    decision: PolicyDecision
    action_class: ActionClass
    reason: str
    requires_authorization_id: Optional[str] = None


class ToolResult(BaseModel):
    call_id: str
    tool: str
    ok: bool
    output: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    error_kind: str = Field(default="", description="transient|structural|denied|unavailable|timeout|")
    trust: TrustLevel = TrustLevel.VERIFIED_TOOL
    duration_ms: int = 0
    verdict: Optional[FirewallVerdict] = None
    injection_flags: list[str] = Field(default_factory=list)
    truncated: bool = False
