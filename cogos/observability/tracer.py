"""Structured tracer: every cognitive step becomes a durable :class:`TraceEvent`.

The tracer is the single answer to "what did the runtime do and why". It
persists to the :class:`StateStore`, optionally mirrors to a JSONL file and/or
stdout, and can reconstruct an explanation of a mission purely from traces.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from cogos.persistence.store import StateStore
from cogos.schemas.trace import TraceEvent

_COST_NUMERIC_KEYS = ("input_tokens", "output_tokens", "cost_usd", "tokens", "duration_ms", "model_calls", "tool_calls")


class Tracer:
    def __init__(
        self,
        store: StateStore,
        mission_id: Optional[str] = None,
        stdout: bool = False,
        jsonl_path: Optional[Path] = None,
    ):
        self.store = store
        self.mission_id = mission_id
        self.stdout = stdout
        self.jsonl_path = Path(jsonl_path) if jsonl_path is not None else None
        self.cycle = 0
        self._seq = 0

    # -- configuration ------------------------------------------------------------

    def set_cycle(self, n: int) -> None:
        self.cycle = int(n)

    def set_mission(self, mission_id: Optional[str]) -> None:
        self.mission_id = mission_id

    # -- emission -----------------------------------------------------------------

    def emit(
        self,
        kind: str,
        summary: str,
        *,
        cycle: int = 0,
        data: Optional[dict[str, Any]] = None,
        cost: Optional[dict[str, Any]] = None,
        parent_id: Optional[str] = None,
    ) -> TraceEvent:
        self._seq += 1
        payload = dict(data or {})
        payload.setdefault("seq", self._seq)
        ev = TraceEvent(
            mission_id=self.mission_id,
            cycle=cycle or self.cycle,
            kind=kind,
            summary=summary,
            data=payload,
            cost=dict(cost or {}),
            parent_id=parent_id,
        )
        self.store.record_trace(ev)
        if self.jsonl_path is not None:
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.jsonl_path, "a", encoding="utf-8") as fh:
                fh.write(ev.model_dump_json() + "\n")
        if self.stdout:
            print(self._format_line(ev), flush=True)
        return ev

    @contextmanager
    def span(self, kind: str, summary: str, *, data: Optional[dict[str, Any]] = None) -> Iterator[TraceEvent]:
        """Emit ``<kind>`` start/end events around a block, capturing duration and errors."""
        start = self.emit(kind, summary, data={"phase": "start", **(data or {})})
        t0 = time.monotonic()
        try:
            yield start
        except BaseException as exc:
            duration = int((time.monotonic() - t0) * 1000)
            self.emit(
                kind,
                f"{summary} failed: {type(exc).__name__}: {exc}",
                data={
                    "phase": "end",
                    "ok": False,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "duration_ms": duration,
                },
                cost={"duration_ms": duration},
                parent_id=start.id,
            )
            raise
        duration = int((time.monotonic() - t0) * 1000)
        self.emit(
            kind,
            f"{summary} done",
            data={"phase": "end", "ok": True, "duration_ms": duration},
            cost={"duration_ms": duration},
            parent_id=start.id,
        )

    # -- explanation --------------------------------------------------------------

    def explain(self, mission_id: str) -> dict[str, Any]:
        """Answer the observability questions for a mission from its traces alone."""
        traces = self._ordered(self.store.traces(mission_id, limit=10_000))
        objective: Optional[str] = None
        compiled: dict[str, Any] = {}
        for t in traces:
            if t.kind == "mission_compiled":
                compiled = t.data
                objective = t.data.get("objective") or objective

        def _by_kind(kind: str) -> list[TraceEvent]:
            return [t for t in traces if t.kind == kind]

        total_cost: dict[str, float] = {}
        for t in traces:
            for k, v in t.cost.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    total_cost[k] = total_cost.get(k, 0.0) + float(v)

        assess_events = _by_kind("assess")
        latest_assess = assess_events[-1].data if assess_events else {}
        uncertainties = (
            latest_assess.get("unresolved_uncertainties")
            or latest_assess.get("uncertainties")
            or latest_assess.get("open_unknowns")
            or []
        )

        return {
            "mission_id": mission_id,
            "objective": objective,
            "compiled": compiled,
            "decisions": [
                {"cycle": t.cycle, "summary": t.summary, **t.data} for t in _by_kind("decision")
            ],
            "operations": [
                {
                    "cycle": t.cycle,
                    "operation": t.data.get("operation"),
                    "rationale": t.data.get("rationale"),
                    "tool": t.data.get("tool"),
                    "summary": t.summary,
                }
                for t in _by_kind("select")
            ],
            "tool_calls": [
                {"cycle": t.cycle, "summary": t.summary, **t.data} for t in _by_kind("tool_call")
            ],
            "specialists": [
                {"cycle": t.cycle, "summary": t.summary, **t.data} for t in _by_kind("specialist")
            ],
            "failures": [
                {"cycle": t.cycle, "summary": t.summary, **t.data} for t in _by_kind("failure")
            ],
            "retries": [
                {"cycle": t.cycle, "summary": t.summary, **t.data} for t in _by_kind("retry")
            ],
            "verifications": [
                {"cycle": t.cycle, "summary": t.summary, **t.data} for t in _by_kind("verify")
            ],
            "total_cost": total_cost,
            "unresolved_uncertainties": uncertainties,
            "latest_assessment": latest_assess,
            "trace_count": len(traces),
            "cycles": max((t.cycle for t in traces), default=0),
        }

    def timeline(self, mission_id: str, limit: int = 200) -> list[str]:
        traces = self._ordered(self.store.traces(mission_id, limit=limit))
        return [self._format_line(t) for t in traces]

    @staticmethod
    def _ordered(traces: list[TraceEvent]) -> list[TraceEvent]:
        """Stable order: timestamp, then the emitter's sequence number when present."""
        def key(t: TraceEvent) -> tuple[str, int]:
            seq = t.data.get("seq")
            return (t.ts, seq if isinstance(seq, int) else 0)
        return sorted(traces, key=key)

    # -- helpers ------------------------------------------------------------------

    @staticmethod
    def _format_line(ev: TraceEvent) -> str:
        cost = ""
        if ev.cost:
            bits = [f"{k}={v}" for k, v in ev.cost.items() if k in _COST_NUMERIC_KEYS or isinstance(v, (int, float))]
            if bits:
                cost = " [" + " ".join(bits) + "]"
        summary = " ".join(ev.summary.split())
        if len(summary) > 160:
            summary = summary[:157] + "..."
        mid = ev.mission_id or "-"
        return f"{ev.ts} c{ev.cycle:03d} {ev.kind:<14} {mid} {summary}{cost}"

    @staticmethod
    def to_json(ev: TraceEvent) -> str:
        return json.dumps(ev.model_dump(), default=str, separators=(",", ":"))


__all__ = ["Tracer"]
