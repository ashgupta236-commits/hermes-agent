"""Decision journal: every consequential choice is recorded, reviewed and scored."""

from __future__ import annotations

from typing import Optional

from cogos.ids import iso_now
from cogos.persistence.store import StateStore
from cogos.schemas.decisions import Decision
from cogos.schemas.mission import MissionState


class DecisionJournal:
    def __init__(self, store: StateStore, state: MissionState):
        self.store = store
        self.state = state

    def record(self, decision: Decision) -> Decision:
        existing = self.get(decision.decision_id)
        if existing is None:
            self.state.decisions.append(decision)
        elif existing is not decision:
            idx = self.state.decisions.index(existing)
            self.state.decisions[idx] = decision
        self.store.record_decision(self.state.mission_id, decision)
        return decision

    def get(self, decision_id: str) -> Optional[Decision]:
        for d in self.state.decisions:
            if d.decision_id == decision_id:
                return d
        return None

    def resolve(self, decision_id: str, actual_outcome: str, success: bool) -> Optional[Decision]:
        decision = self.get(decision_id)
        if decision is None:
            return None
        decision.actual_outcome = actual_outcome
        decision.outcome_success = bool(success)
        self.store.record_decision(self.state.mission_id, decision)
        self.store.record_calibration(
            self.state.mission_id,
            domain=decision.domain,
            predicted=decision.confidence,
            outcome=bool(success),
            ref_id=decision.decision_id,
        )
        return decision

    def pending_reviews(self, now: Optional[str] = None) -> list[Decision]:
        """Decisions carrying a review trigger whose outcome is still unknown.

        ``now`` is accepted for callers that want to filter by time-based triggers
        (ISO timestamps embedded in ``review_trigger``); triggers that are not
        timestamps are always considered pending.
        """
        now = now or iso_now()
        out: list[Decision] = []
        for d in self.state.decisions:
            if not d.review_trigger or d.actual_outcome is not None:
                continue
            trigger = d.review_trigger.strip()
            if _looks_like_timestamp(trigger) and trigger > now:
                continue
            out.append(d)
        return out

    def consequential(self) -> list[Decision]:
        return [d for d in self.state.decisions if d.consequential or d.reversibility == "irreversible"]

    def unresolved(self) -> list[Decision]:
        return [d for d in self.state.decisions if d.actual_outcome is None]


def _looks_like_timestamp(text: str) -> bool:
    return len(text) >= 10 and text[:4].isdigit() and text[4] == "-" and text[7] == "-"
