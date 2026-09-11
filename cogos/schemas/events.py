"""Event schema for event-driven operation."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from cogos.ids import iso_now, new_id


class Event(BaseModel):
    id: str = Field(default_factory=lambda: new_id("evt"))
    kind: str = Field(description="test_completed|job_completed|file_changed|db_changed|message|webhook|scheduled|deadline|threshold|new_evidence|human_input|custom")
    source: str = "system"
    payload: dict[str, Any] = Field(default_factory=dict)
    mission_ids: list[str] = Field(default_factory=list, description="Explicit targets; empty means route by subscription")
    occurred_at: str = Field(default_factory=iso_now)
    trusted: bool = Field(default=False, description="Only human/system events are trusted instructions; others are data")
    handled: bool = False
    handled_at: Optional[str] = None
    routed_to: list[str] = Field(default_factory=list)
