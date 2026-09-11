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
        self.reserved_calls = 0
        self.reserved_cost_usd = 0.0

    # -- accounting -----------------------------------------------------------------

    def add_model_call(self, resp: CognitionResponse) -> None:
        """Account for a cognition call *and every retry behind it*.

        A response that took three attempts cost three attempts. Charging the budget for one
        makes the ledger optimistic exactly when the run is going badly, which is when an
        accurate remaining-budget figure matters most.
        """
        attempts = max(1, int(getattr(resp, "attempts", 1) or 1))
        records = list(getattr(resp, "attempt_records", []) or [])
        self.usage.model_calls += attempts
        self.usage.retries += attempts - 1
        if records:
            self.usage.input_tokens += sum(int(a.input_tokens or 0) for a in records)
            self.usage.output_tokens += sum(int(a.output_tokens or 0) for a in records)
            self.usage.estimated_cost_usd += sum(float(a.cost_usd or 0.0) for a in records)
            self.usage.wall_clock_seconds += sum(float(a.duration_ms or 0) for a in records) / 1000.0
        else:
            self.usage.input_tokens += int(resp.input_tokens or 0)
            self.usage.output_tokens += int(resp.output_tokens or 0)
            self.usage.estimated_cost_usd += float(resp.cost_usd or 0.0)
            self.usage.wall_clock_seconds += float(resp.duration_ms or 0) / 1000.0
        self.release(calls=attempts)

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

    # -- reservation ------------------------------------------------------------------

    def reserve(self, calls: int = 1, cost_usd: float = 0.0) -> None:
        """Hold budget for a call that is about to be made but has not been accounted yet.

        Without this, a call in flight is invisible to :meth:`over_budget`, so a run can
        authorise work it can no longer pay for. The reservation is released when the call is
        accounted (:meth:`add_model_call`) or abandoned (:meth:`release`).
        """
        self.reserved_calls += max(0, int(calls))
        self.reserved_cost_usd += max(0.0, float(cost_usd))

    def release(self, calls: Optional[int] = None, cost_usd: Optional[float] = None) -> None:
        """Drop part or all of an outstanding reservation.

        Called with no arguments the whole reservation is dropped (the work was abandoned);
        with amounts, only the part that has now been really accounted for.
        """
        if calls is None and cost_usd is None:
            self.reserved_calls = 0
            self.reserved_cost_usd = 0.0
            return
        if calls is not None:
            self.reserved_calls = max(0, self.reserved_calls - int(calls))
        if cost_usd is not None:
            self.reserved_cost_usd = max(0.0, self.reserved_cost_usd - float(cost_usd))

    def add_wall_clock(self, seconds: float) -> None:
        self.usage.wall_clock_seconds += float(seconds)

    # -- budget -------------------------------------------------------------------------

    def over_budget(self, budget: Budget) -> Optional[str]:
        """Return a human-readable breach reason, or None when within budget."""
        u = self.usage
        calls = u.model_calls + self.reserved_calls
        cost = u.estimated_cost_usd + self.reserved_cost_usd
        if u.cycles >= budget.max_cycles:
            return f"cycle budget exhausted: {u.cycles}/{budget.max_cycles} cycles used"
        if calls >= budget.max_model_calls:
            held = f" ({self.reserved_calls} reserved)" if self.reserved_calls else ""
            return f"model-call budget exhausted: {calls}/{budget.max_model_calls} calls used{held}"
        if u.subagents_spawned >= budget.max_subagents:
            return f"subagent budget exhausted: {u.subagents_spawned}/{budget.max_subagents} specialists spawned"
        if budget.max_cost_usd is not None and cost >= budget.max_cost_usd:
            return f"cost budget exhausted: ${cost:.2f} of ${budget.max_cost_usd:.2f} committed"
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
            "model_calls": budget.max_model_calls - u.model_calls - self.reserved_calls,
            "subagents": budget.max_subagents - u.subagents_spawned,
        }
        if budget.max_cost_usd is not None:
            out["cost_usd"] = round(budget.max_cost_usd - u.estimated_cost_usd - self.reserved_cost_usd, 4)
        if budget.max_wall_clock_seconds is not None:
            out["wall_clock_seconds"] = round(budget.max_wall_clock_seconds - u.wall_clock_seconds, 1)
        return out

    # -- admission control ---------------------------------------------------------------

    def admit(self, budget: Budget, *, estimated_cost_usd: float = 0.0, estimated_seconds: float = 0.0) -> Optional[str]:
        """Can the next operation be *started* within what is left? Returns a refusal, or None.

        The live run checked the budget at cycle start, then began a call that ran for 486 seconds
        and $2.53 — so a 2400s cap became 2940s and a $19.00 cap became $20.51. Checking whether
        the work already done fits is not the same question as whether the next piece will.

        Refusing is the only safe direction: it stops the mission, it never completes it. Nothing
        here may downgrade the executive model to make a call fit — model residency is a safety
        property, not a budget lever.
        """
        breach = self.over_budget(budget)
        if breach:
            return breach
        u = self.usage
        if budget.max_cost_usd is not None and estimated_cost_usd > 0:
            committed = u.estimated_cost_usd + self.reserved_cost_usd + estimated_cost_usd
            if committed > budget.max_cost_usd:
                return (
                    f"next operation would exceed the cost budget: ${u.estimated_cost_usd:.2f} spent"
                    f"{f' + ${self.reserved_cost_usd:.2f} reserved' if self.reserved_cost_usd else ''}"
                    f" + ${estimated_cost_usd:.2f} estimated > ${budget.max_cost_usd:.2f}"
                )
        if budget.max_wall_clock_seconds is not None and estimated_seconds > 0:
            committed_s = u.wall_clock_seconds + estimated_seconds
            if committed_s > budget.max_wall_clock_seconds:
                return (
                    f"next operation would exceed the wall-clock budget: {u.wall_clock_seconds:.0f}s elapsed"
                    f" + {estimated_seconds:.0f}s estimated > {budget.max_wall_clock_seconds:.0f}s"
                )
        return None

    def affordable_cost(self, budget: Budget) -> Optional[float]:
        """The most the next single call may cost, or None when cost is unbounded."""
        if budget.max_cost_usd is None:
            return None
        return max(0.0, budget.max_cost_usd - self.usage.estimated_cost_usd - self.reserved_cost_usd)

    def snapshot(self) -> dict[str, Any]:
        d = self.usage.model_dump()
        d["total_tokens"] = self.usage.input_tokens + self.usage.output_tokens
        d["reserved_calls"] = self.reserved_calls
        d["reserved_cost_usd"] = round(self.reserved_cost_usd, 6)
        return d
