"""Structured tracing schema."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from cogos.ids import iso_now, new_id


class TraceEvent(BaseModel):
    id: str = Field(default_factory=lambda: new_id("trc"))
    mission_id: Optional[str] = None
    cycle: int = 0
    kind: str = Field(description="cycle_start|perceive|update|assess|select|operation|tool_call|specialist|verify|learn|checkpoint|decision|failure|retry|event|blocked|complete|error")
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    cost: dict[str, Any] = Field(default_factory=dict)
    ts: str = Field(default_factory=iso_now)
    parent_id: Optional[str] = None
