"""Adapter continuity contract (R3).

Three genuinely different things get called "the agent remembers":

``native_session``
    The provider keeps the executive's own session and continues it. Internal reasoning state
    is preserved by the provider under its own contract.
``provider_message_history``
    No session, but the full message history (including any opaque continuity blocks the
    provider returns) is resent, with the stable prompt prefix and correct tool-result pairing.
``external_state_only``
    Neither. Continuity comes entirely from cogos' durable mission state: objectives, criteria,
    observations, explicit beliefs, decision summaries, unresolved alternatives, task
    dependencies, tool receipts, resource balances and next action.

The headless `claude -p` adapter runs with ``--no-session-persistence``, so it is
``external_state_only``. That is a useful fallback and it is *not* equivalent to preserving the
executive's internal reasoning — this module exists so the runtime records which mode is
actually active instead of implying the strongest one.

The anchor deliberately has the opposite policy: it excludes commitment history by design. Two
different context policies for two different jobs, not an argument for making everything fresh
or everything persistent.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class ContinuityMode(str, Enum):
    NATIVE_SESSION = "native_session"
    PROVIDER_MESSAGE_HISTORY = "provider_message_history"
    EXTERNAL_STATE_ONLY = "external_state_only"


class ContinuityContract(BaseModel):
    """What an adapter actually supports, and what it therefore does not preserve."""

    mode: ContinuityMode = ContinuityMode.EXTERNAL_STATE_ONLY
    adapter: str = ""
    preserves_internal_reasoning: bool = False
    stable_prompt_prefix: bool = True
    tool_result_pairing: bool = False
    opaque_blocks_supported: bool = False
    detected_from: str = Field(default="", description="How the mode was established, so it is not a guess presented as a fact")
    limitations: list[str] = Field(default_factory=list)

    def describe(self) -> str:
        base = f"{self.mode.value} ({self.adapter})"
        if self.preserves_internal_reasoning:
            return base + "; provider preserves the executive's reasoning state"
        return base + "; internal reasoning is NOT preserved — continuity is reconstructed from durable mission state"


#: What external-state continuity must carry to be worth the name. Checked, not assumed.
REQUIRED_HANDOFF_KEYS = (
    "objective",
    "success_criteria",
    "observations",
    "beliefs",
    "decision_summaries",
    "unresolved_alternatives",
    "task_dependencies",
    "tool_receipts",
    "resource_balances",
    "next_action",
)


def detect(adapter: Any) -> ContinuityContract:
    """Establish the continuity mode from the adapter itself, never from a default assumption."""
    name = str(getattr(adapter, "name", type(adapter).__name__))
    declared = getattr(adapter, "continuity_mode", None)
    if declared is not None:
        mode = ContinuityMode(str(getattr(declared, "value", declared)))
        return ContinuityContract(
            mode=mode,
            adapter=name,
            preserves_internal_reasoning=mode is ContinuityMode.NATIVE_SESSION,
            tool_result_pairing=mode is not ContinuityMode.EXTERNAL_STATE_ONLY,
            opaque_blocks_supported=mode is not ContinuityMode.EXTERNAL_STATE_ONLY,
            detected_from=f"adapter declared continuity_mode={mode.value}",
        )
    if name == "claude_code":
        args = list(getattr(adapter, "extra_args", []) or [])
        no_persist = "--no-session-persistence" in args or True  # build_command always sets it
        return ContinuityContract(
            mode=ContinuityMode.EXTERNAL_STATE_ONLY,
            adapter=name,
            preserves_internal_reasoning=False,
            detected_from="headless CLI is invoked with --no-session-persistence" if no_persist else "headless CLI invocation flags",
            limitations=[
                "each cognition call is a fresh process: the executive's internal reasoning between cycles is not preserved",
                "continuity is only as good as what the runtime wrote to durable state before the call",
            ],
        )
    return ContinuityContract(
        mode=ContinuityMode.EXTERNAL_STATE_ONLY,
        adapter=name,
        detected_from=f"adapter '{name}' declares no continuity contract; assuming the weakest mode rather than the most convenient one",
        limitations=[f"continuity support for adapter '{name}' has not been established"],
    )


def handoff(state: Any, next_action: str = "") -> dict[str, Any]:
    """The versioned handoff written at compaction, with source pointers rather than prose.

    Everything here is a pointer into durable state, so a resumed run reconciles against the
    environment instead of trusting a summary of it.
    """
    return {
        "version": 1,
        "mission_id": state.mission_id,
        "state_revision": state.version,
        "objective": state.objective,
        "success_criteria": [{"id": c.id, "description": c.description, "satisfied": c.satisfied, "verification_ids": list(c.verification_ids)} for c in state.success_criteria],
        "observations": [o.id for o in state.observations[-50:]],
        "beliefs": [{"id": c.id, "proposition": c.proposition[:200], "status": c.status.value, "confidence": c.confidence} for c in state.claims[-30:]],
        "decision_summaries": [d.decision_id for d in state.decisions[-20:]],
        "unresolved_alternatives": [h.statement[:200] for h in state.hypotheses if h.status not in ("refuted",)][:10],
        "task_dependencies": [{"id": t.id, "status": t.status.value, "depends_on": list(t.depends_on)} for t in state.tasks],
        "tool_receipts": [v.id for v in state.verifications[-30:]],
        "resource_balances": state.usage.model_dump(),
        "holds": [{"id": h.id, "branch": h.branch, "status": h.status.value, "rounds": h.rounds} for h in state.holds],
        "next_action": next_action,
    }


def handoff_complete(payload: dict[str, Any]) -> tuple[bool, list[str]]:
    """A handoff missing any required key is incomplete, and says which."""
    missing: list[str] = [k for k in REQUIRED_HANDOFF_KEYS if k not in payload]
    return (not missing), missing


def anchor_context_policy(contract: ContinuityContract) -> dict[str, Any]:
    """The anchor's context policy: the inverse of the executive's, whatever mode is active."""
    return {
        "mode": ContinuityMode.EXTERNAL_STATE_ONLY.value,
        "reason": "the anchor must not inherit the executive's commitment history",
        "executive_mode": contract.mode.value,
        "carries_executive_history": False,
        "residual_risk": "a fresh provider process may still load project-level context (CLAUDE.md, skills, MCP config); "
        "context independence is not statistical independence",
    }
