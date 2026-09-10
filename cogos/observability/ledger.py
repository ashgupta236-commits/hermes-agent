"""Resource ledger: tracks consumption against a :class:`Budget`."""

from __future__ import annotations

from typing import Any, Optional

from cogos.adapters.base import CognitionResponse
from cogos.schemas.mission import Budget, ResourceUsage
from cogos.schemas.tools import ToolResult

_NETWORK_TOOLS = ("web_fetch", "web_search")


class ResourceLedger:
    def __init__(self, usage: ResourceUsage):
        self.usage = usage

    # -- accounting -----------------------------------------------------------------

    def add_model_call(self, resp: CognitionResponse) -> None:
        self.usage.model_calls += 1
        self.usage.input_tokens += int(resp.input_tokens or 0)
        self.usage.output_tokens += int(resp.output_tokens or 0)
        self.usage.estimated_cost_usd += float(resp.cost_usd or 0.0)
        self.usage.wall_clock_seconds += float(resp.duration_ms or 0) / 1000.0

    def add_tool_call(self, res: ToolResult) -> None:
        self.usage.tool_calls += 1
        if res.tool in _NETWORK_TOOLS or bool(res.data.get("network")):
            self.usage.network_requests += 1

    def add_retry(self) -> None:
        self.usage.retries += 1

    def add_subagent(self, n: int = 1) -> None:
        self.usage.subagents_spawned += int(n)

    def add_cycle(self) -> None:
        self.usage.cycles += 1

    def add_wall_clock(self, seconds: float) -> None:
        self.usage.wall_clock_seconds += float(seconds)

    # -- budget -------------------------------------------------------------------------

    def over_budget(self, budget: Budget) -> Optional[str]:
        """Return a human-readable breach reason, or None when within budget."""
        u = self.usage
        if u.cycles >= budget.max_cycles:
            return f"cycle budget exhausted: {u.cycles}/{budget.max_cycles} cycles used"
        if u.model_calls >= budget.max_model_calls:
            return f"model-call budget exhausted: {u.model_calls}/{budget.max_model_calls} calls used"
        if u.subagents_spawned >= budget.max_subagents:
            return f"subagent budget exhausted: {u.subagents_spawned}/{budget.max_subagents} specialists spawned"
        if budget.max_cost_usd is not None and u.estimated_cost_usd >= budget.max_cost_usd:
            return f"cost budget exhausted: ${u.estimated_cost_usd:.2f} of ${budget.max_cost_usd:.2f} spent"
        if budget.max_wall_clock_seconds is not None and u.wall_clock_seconds >= budget.max_wall_clock_seconds:
            return (
                f"wall-clock budget exhausted: {u.wall_clock_seconds:.0f}s of "
                f"{budget.max_wall_clock_seconds:.0f}s elapsed"
            )
        return None

    def remaining(self, budget: Budget) -> dict[str, Any]:
        u = self.usage
        out: dict[str, Any] = {
            "cycles": budget.max_cycles - u.cycles,
            "model_calls": budget.max_model_calls - u.model_calls,
            "subagents": budget.max_subagents - u.subagents_spawned,
        }
        if budget.max_cost_usd is not None:
            out["cost_usd"] = round(budget.max_cost_usd - u.estimated_cost_usd, 4)
        if budget.max_wall_clock_seconds is not None:
            out["wall_clock_seconds"] = round(budget.max_wall_clock_seconds - u.wall_clock_seconds, 1)
        return out

    def snapshot(self) -> dict[str, Any]:
        d = self.usage.model_dump()
        d["total_tokens"] = self.usage.input_tokens + self.usage.output_tokens
        return d
