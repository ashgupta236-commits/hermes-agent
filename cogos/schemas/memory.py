"""Memory schemas."""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from cogos.ids import iso_now, new_id
from cogos.schemas.common import Provenance


class MemoryClass(str, Enum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    RELATIONAL = "relational"
    CAUSAL = "causal"
    FAILURE = "failure"
    TEMPORAL = "temporal"
    META = "meta"


class MemoryRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("mem"))
    memory_class: MemoryClass
    content: str
    tags: list[str] = Field(default_factory=list)
    mission_id: Optional[str] = None
    provenance: Provenance = Field(default_factory=lambda: Provenance(source="system"))
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    expires_at: Optional[str] = None
    version: int = 1
    superseded_by: Optional[str] = None
    contradicts: list[str] = Field(default_factory=list)
    access_count: int = 0
    last_accessed_at: Optional[str] = None
    created_at: str = Field(default_factory=iso_now)
    updated_at: str = Field(default_factory=iso_now)
    data: dict[str, Any] = Field(default_factory=dict)
    content_hash: str = ""
