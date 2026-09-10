"""Enumerations and small value objects used across schemas."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from cogos.ids import iso_now


class EpistemicStatus(str, Enum):
    """How a piece of knowledge was obtained. Never silently converted."""

    OBSERVATION = "observation"
    INFERENCE = "inference"
    ASSUMPTION = "assumption"
    PREDICTION = "prediction"
    HYPOTHESIS = "hypothesis"
    ESTABLISHED_FACT = "established_fact"


class TrustLevel(str, Enum):
    """Who or what produced a piece of content. Governs instruction authority."""

    HUMAN_PRINCIPAL = "human_principal"  # the mission owner
    SYSTEM = "system"  # runtime-generated
    VERIFIED_TOOL = "verified_tool"  # deterministic tool output (tests, calc)
    SPECIALIST = "specialist"  # a spawned cognitive process
    UNTRUSTED_EXTERNAL = "untrusted_external"  # web, files from others, tool text


class ActionClass(str, Enum):
    REVERSIBLE_LOCAL = "reversible_local"
    REVERSIBLE_EXTERNAL = "reversible_external"
    CONSEQUENTIAL_SHARED = "consequential_shared"
    DESTRUCTIVE = "destructive"
    FINANCIAL = "financial"
    SECURITY_SENSITIVE = "security_sensitive"
    PRIVACY_SENSITIVE = "privacy_sensitive"
    LEGALLY_SIGNIFICANT = "legally_significant"
    CREDENTIAL_SENSITIVE = "credential_sensitive"


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_HUMAN = "require_human"


class VerificationStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    SKIPPED = "skipped"


class OperationKind(str, Enum):
    """The executive's action space for one cycle."""

    DIRECT_REASONING = "direct_reasoning"
    RETRIEVE_MEMORY = "retrieve_memory"
    SEARCH = "search"
    INSPECT_FILES = "inspect_files"
    EXECUTE_CODE = "execute_code"
    RUN_EXPERIMENT = "run_experiment"
    CALCULATE = "calculate"
    SIMULATE = "simulate"
    INSTANTIATE_SPECIALIST = "instantiate_specialist"
    PARALLEL_WORKSTREAMS = "parallel_workstreams"
    USE_EXTERNAL_TOOL = "use_external_tool"
    EXECUTE_ACTION = "execute_action"
    VERIFY = "verify"
    FALSIFY = "falsify"
    SYNTHESIZE = "synthesize"
    WAIT_FOR_EXTERNAL_EVENT = "wait_for_external_event"
    REQUEST_HUMAN_AUTHORIZATION = "request_human_authorization"
    COMPLETE_MISSION = "complete_mission"


class Provenance(BaseModel):
    """Where something came from, when, and how much it can be trusted."""

    source: str = Field(description="URL, path, tool name, specialist id, or 'human'")
    trust: TrustLevel = TrustLevel.SYSTEM
    acquired_at: str = Field(default_factory=iso_now)
    method: str = Field(default="", description="How it was acquired (tool, reasoning, human input)")
    reliability: float = Field(default=0.5, ge=0.0, le=1.0)
    trace_id: Optional[str] = None
    lineage: list[str] = Field(
        default_factory=list,
        description="Root/original sources this item ultimately derives from (for independence checks)",
    )


class Timestamps(BaseModel):
    created_at: str = Field(default_factory=iso_now)
    updated_at: str = Field(default_factory=iso_now)

    def touch(self) -> None:
        self.updated_at = iso_now()
