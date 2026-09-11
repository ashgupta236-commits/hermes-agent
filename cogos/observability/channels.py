"""Recording and reconciling the four decision channels (R2).

:class:`ChannelRecorder` assembles one :class:`DecisionRecord` per material decision and then
*reconciles* it: comparing what was declared against what actually ran, what ran against what
the environment showed, and both against what the anchor concluded. Every difference becomes a
:class:`Discrepancy` — including the quiet ones, like a check the executive said it performed
for which no tool ever executed.

The records are appended to a hash-chained log. That chain detects a record edited in place; it
does **not** make the log tamper-proof against a writer that can rewrite the whole chain and its
root, which is why the authoritative copy lives in the store (outside candidate write authority)
and why :func:`verify_chain` reports what it actually checked rather than asserting integrity.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from cogos.ids import iso_now
from cogos.schemas.channels import (
    AnchorChannel,
    AttemptedChannel,
    ChannelName,
    DecisionRecord,
    DeclaredChannel,
    Discrepancy,
    DiscrepancyKind,
    EnvironmentChannel,
    ModelAttempt,
    ToolAttempt,
)

CHAIN_KEY = "decision_chain"
GENESIS = "0" * 64


def link_hash(previous: str, record: DecisionRecord) -> str:
    body = json.dumps(record.model_dump(mode="json"), sort_keys=True, default=str)
    return hashlib.sha256((previous + body).encode("utf-8")).hexdigest()


class ChannelRecorder:
    def __init__(self, store: Any = None, run_id: str = ""):
        self.store = store
        self.run_id = run_id
        self.records: list[DecisionRecord] = []

    # -- assembly ---------------------------------------------------------------------

    def begin(self, state: Any, decision: Any, cycle: int, branch: str = "mission") -> DecisionRecord:
        expected = list(getattr(decision, "verification_plan", None) or [])
        if not expected and getattr(decision, "rationale", ""):
            expected = []
        rec = DecisionRecord(
            run_id=self.run_id,
            mission_id=getattr(state, "mission_id", ""),
            branch=branch,
            cycle=cycle,
            state_revision_start=int(getattr(state, "version", 0) or 0),
            declared=DeclaredChannel(
                operation=str(getattr(getattr(decision, "operation", ""), "value", getattr(decision, "operation", ""))),
                rationale=str(getattr(decision, "rationale", ""))[:500],
                task_id=getattr(decision, "task_id", None) or None,
                expected_checks=expected,
                consequential=bool(getattr(decision, "consequential", False)),
            ),
            attempted=AttemptedChannel(started_at=iso_now()),
        )
        self.records.append(rec)
        return rec

    @staticmethod
    def record_model_call(rec: DecisionRecord, kind: str, resp: Any) -> None:
        rec.attempted.model_calls.append(
            ModelAttempt(
                kind=kind,
                model_requested=getattr(resp, "model_requested", ""),
                models_used=list(getattr(resp, "models_used", []) or []),
                residency_status=str(getattr(getattr(resp, "residency_status", ""), "value", getattr(resp, "residency_status", "unknown"))),
                attempts=int(getattr(resp, "attempts", 1) or 1),
                ok=bool(getattr(resp, "ok", False)),
                error_kind=str(getattr(resp, "error_kind", "")),
                cost_usd=float(getattr(resp, "cost_usd", 0.0) or 0.0),
                input_tokens=int(getattr(resp, "input_tokens", 0) or 0),
                output_tokens=int(getattr(resp, "output_tokens", 0) or 0),
                duration_ms=int(getattr(resp, "duration_ms", 0) or 0),
            )
        )

    @staticmethod
    def record_tool_call(rec: DecisionRecord, res: Any) -> None:
        """Captured at the execution boundary: what the tool result actually reported.

        `observed_effect` is the tool's own output, not a summary of intent. A result with no
        output and no error leaves it empty, which reconciliation treats as missing telemetry.
        """
        verdict = getattr(res, "verdict", None)
        rec.attempted.tool_calls.append(
            ToolAttempt(
                tool=str(getattr(res, "tool", "")),
                ok=bool(getattr(res, "ok", False)),
                error_kind=str(getattr(res, "error_kind", "") or ""),
                action_class=str(getattr(getattr(verdict, "action_class", ""), "value", "")) if verdict else "",
                verdict=str(getattr(getattr(verdict, "decision", ""), "value", "")) if verdict else "",
                observed_effect=(str(getattr(res, "output", "")) or str(getattr(res, "error", "")))[:400],
                receipt_id=getattr(res, "id", None),
            )
        )

    @staticmethod
    def record_environment(rec: DecisionRecord, state: Any, since_observation: int = 0, since_test: int = 0, since_verification: int = 0) -> None:
        rec.environment = EnvironmentChannel(
            observation_ids=[o.id for o in getattr(state, "observations", [])[since_observation:]],
            artifact_hashes={a.name: (a.verified_hash or a.content_hash or "") for a in getattr(state, "artifacts", []) if (a.verified_hash or a.content_hash)},
            test_records=[f"{t.name}={t.status.value}" for t in getattr(state, "tests", [])[since_test:]],
            verification_ids=[v.id for v in getattr(state, "verifications", [])[since_verification:]],
        )

    @staticmethod
    def record_anchor(rec: DecisionRecord, outcome: Any) -> None:
        rec.anchor = AnchorChannel(
            assessment_id=getattr(getattr(outcome, "assessment", None), "id", None),
            verdict=str(getattr(getattr(getattr(outcome, "assessment", None), "verdict", ""), "value", "")),
            hold_id=getattr(getattr(outcome, "hold", None), "id", None),
            disagreement_kinds=[str(getattr(d.kind, "value", d.kind)) for d in getattr(outcome, "disagreements", [])],
            max_materiality=max((float(d.materiality) for d in getattr(outcome, "disagreements", [])), default=0.0),
        )

    # -- reconciliation ---------------------------------------------------------------

    def finish(self, rec: DecisionRecord, state: Any) -> DecisionRecord:
        rec.state_revision_end = int(getattr(state, "version", 0) or 0)
        rec.attempted.ended_at = iso_now()
        rec.discrepancies.extend(reconcile(rec))
        rec.unknown_fields = unknowns(rec)
        self._append(rec)
        return rec

    def _append(self, rec: DecisionRecord) -> None:
        if self.store is None:
            return
        chain: dict[str, Any] = self.store.kv_get(CHAIN_KEY, {}) or {}
        head = str(chain.get("head") or GENESIS)
        link = link_hash(head, rec)
        entries = list(chain.get("entries") or [])
        entries.append({"decision_id": rec.id, "link": link, "at": rec.created_at})
        self.store.kv_set(CHAIN_KEY, {"head": link, "entries": entries[-2000:], "count": int(chain.get("count") or 0) + 1})


def reconcile(rec: DecisionRecord) -> list[Discrepancy]:
    """Compare the channels against one another and raise every difference."""
    out: list[Discrepancy] = []

    # A check the executive said it performed, with nothing at the execution boundary for it.
    if rec.declared.expected_checks and not rec.attempted.tool_calls:
        out.append(
            Discrepancy(
                kind=DiscrepancyKind.CLAIMED_BUT_UNOBSERVED,
                detail="declared checks with no tool execution behind them: " + "; ".join(rec.declared.expected_checks[:3]),
                channels=[ChannelName.DECLARED, ChannelName.ATTEMPTED],
            )
        )

    # A tool that reported success while reporting no effect at all.
    for call in rec.attempted.tool_calls:
        if call.ok and not call.observed_effect:
            out.append(
                Discrepancy(
                    kind=DiscrepancyKind.MISSING_TELEMETRY,
                    detail=f"tool '{call.tool}' reported success with no observable result from the execution boundary",
                    severity=0.6,
                    channels=[ChannelName.ATTEMPTED, ChannelName.ENVIRONMENT],
                )
            )

    for call in rec.attempted.model_calls:
        if call.residency_status == "mismatch":
            out.append(
                Discrepancy(
                    kind=DiscrepancyKind.IDENTITY_MISMATCH,
                    detail=f"{call.kind}: requested {call.model_requested}, served by {', '.join(call.models_used) or 'unknown'}",
                    severity=1.0,
                    channels=[ChannelName.ATTEMPTED],
                )
            )
        elif call.residency_status == "unknown":
            out.append(
                Discrepancy(
                    kind=DiscrepancyKind.MISSING_TELEMETRY,
                    detail=f"{call.kind}: no model telemetry, so the serving model was not established",
                    severity=0.4,
                    channels=[ChannelName.ATTEMPTED],
                )
            )

    if rec.anchor.hold_id or rec.anchor.disagreement_kinds:
        # Severity follows materiality: an anchor that *refutes* the position is a
        # high-priority conflict, while one that merely could not corroborate what the runtime
        # verified deterministically is a recorded limitation, not an alarm.
        material = bool(rec.anchor.hold_id) or rec.anchor.max_materiality >= 0.5
        out.append(
            Discrepancy(
                kind=DiscrepancyKind.ANCHOR_CONFLICT,
                detail=("blind assessment disagreed: " if material else "blind assessment could not corroborate: ")
                + ", ".join(rec.anchor.disagreement_kinds[:4]),
                severity=1.0 if material else 0.3,
                channels=[ChannelName.DECLARED, ChannelName.ANCHOR],
            )
        )

    # Work that changed mission state without anything showing up in the environment.
    changed = rec.state_revision_end > rec.state_revision_start
    if changed and rec.attempted.tool_calls and not rec.environment.present():
        out.append(
            Discrepancy(
                kind=DiscrepancyKind.UNEXPECTED_EFFECT,
                detail="mission state advanced on tool activity with no environmental record of it",
                severity=0.5,
                channels=[ChannelName.ATTEMPTED, ChannelName.ENVIRONMENT],
            )
        )
    return out


def unknowns(rec: DecisionRecord) -> list[str]:
    """Channels with no data. An empty channel is an unknown, never an implied success."""
    out: list[str] = []
    if not rec.attempted.telemetry_present():
        out.append("attempted: no model or tool telemetry for this decision")
    if not rec.environment.present():
        out.append("environment: nothing independently observed for this decision")
    if not rec.anchor.assessment_id:
        out.append("anchor: no blind assessment covers this decision")
    return out


def verify_chain(store: Any, records: list[DecisionRecord]) -> dict[str, Any]:
    """Re-derive the hash chain from the records and report exactly what was checked.

    A mismatch proves a record changed after it was linked. A match proves only that the
    records and the chain agree — a writer able to rewrite both would produce a match too, so
    the result says so instead of reporting "integrity verified".
    """
    chain: dict[str, Any] = (store.kv_get(CHAIN_KEY, {}) or {}) if store is not None else {}
    entries = list(chain.get("entries") or [])
    by_id = {r.id: r for r in records}
    head = GENESIS
    broken: list[str] = []
    checked = 0
    for entry in entries:
        rec = by_id.get(entry.get("decision_id"))
        if rec is None:
            broken.append(f"{entry.get('decision_id')}: record not present to re-derive")
            head = str(entry.get("link"))
            continue
        expected = link_hash(head, rec)
        if expected != entry.get("link"):
            broken.append(f"{rec.id}: link does not re-derive")
        head = str(entry.get("link"))
        checked += 1
    return {
        "entries": len(entries),
        "records_rederived": checked,
        "broken": broken,
        "head_matches": head == str(chain.get("head") or GENESIS) if entries else True,
        "guarantee": "detects a record edited after linking; does not protect against a writer that can rewrite the chain and its root",
    }
