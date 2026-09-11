"""Event bus: durable, routed, acknowledged delivery of :class:`Event` objects.

Routing rules:
* Explicit ``event.mission_ids`` always receive the event.
* Subscriptions whose ``event_kind`` equals the event kind (or ``'*'``) and
  whose ``filter`` dict is a subset-match of ``event.payload`` receive it too.
* Only ``human`` and ``system`` sources may be trusted; every other source is
  forced to ``trusted=False`` regardless of what the event claims.

Acknowledgements are stored under ``payload['_acks']``; the event is marked
handled once every routed mission has acknowledged it.
"""

from __future__ import annotations

from typing import Any, Optional

from cogos.ids import iso_now
from cogos.persistence.store import StateStore
from cogos.schemas.events import Event

TRUSTED_SOURCES = ("human", "system")
ACK_KEY = "_acks"


def filter_matches(filter_: Optional[dict[str, Any]], payload: dict[str, Any]) -> bool:
    """True when every key in ``filter_`` is present in ``payload`` with an equal value.

    Nested dict filters recurse; list filters require every listed value to be
    present in the payload list.
    """
    if not filter_:
        return True
    for key, expected in filter_.items():
        if key not in payload:
            return False
        actual = payload[key]
        if isinstance(expected, dict) and isinstance(actual, dict):
            if not filter_matches(expected, actual):
                return False
        elif isinstance(expected, list) and isinstance(actual, list):
            if not all(item in actual for item in expected):
                return False
        elif actual != expected:
            return False
    return True


class EventBus:
    def __init__(self, store: StateStore):
        self.store = store

    # -- publishing -----------------------------------------------------------------

    def emit(self, event: Event) -> Event:
        if event.source not in TRUSTED_SOURCES:
            event.trusted = False
        targets: list[str] = list(event.mission_ids)
        for sub in self._matching_subscriptions(event):
            if sub["mission_id"] not in targets:
                targets.append(sub["mission_id"])
        event.routed_to = targets
        event.payload.setdefault(ACK_KEY, [])
        if not targets:
            # Nothing to deliver to: persist as already handled for audit purposes.
            event.handled = True
            event.handled_at = iso_now()
        self.store.put_event(event)
        return event

    def subscribe(
        self,
        mission_id: str,
        event_kind: str,
        filter_: Optional[dict[str, Any]] = None,
        affects: Optional[dict[str, Any]] = None,
    ) -> None:
        self.store.subscribe(mission_id, event_kind, filter_ or {}, affects or {})

    # -- consumption ----------------------------------------------------------------

    def pending_for(self, mission_id: str) -> list[Event]:
        out: list[Event] = []
        for ev in self.store.pending_events():
            if mission_id in ev.routed_to and mission_id not in self._acks(ev):
                out.append(ev)
        return out

    def pending(self) -> list[Event]:
        return self.store.pending_events()

    def mark_handled(self, event_id: str, mission_id: str) -> Optional[Event]:
        for ev in self.store.pending_events():
            if ev.id != event_id:
                continue
            acks = self._acks(ev)
            if mission_id not in acks:
                acks.append(mission_id)
            ev.payload[ACK_KEY] = acks
            if all(m in acks for m in ev.routed_to):
                ev.handled = True
                ev.handled_at = iso_now()
            self.store.put_event(ev)
            return ev
        return None

    def wake_targets(self) -> list[str]:
        targets: set[str] = set()
        for ev in self.store.pending_events():
            acks = self._acks(ev)
            for m in ev.routed_to:
                if m not in acks:
                    targets.add(m)
        return sorted(targets)

    def affected_state(self, event: Event, mission_id: str) -> dict[str, list[str]]:
        """Merge the ``affects`` maps of every subscription that routed ``event`` to ``mission_id``."""
        merged: dict[str, list[str]] = {"claims": [], "unknowns": [], "tasks": []}
        for sub in self._matching_subscriptions(event):
            if sub["mission_id"] != mission_id:
                continue
            for key, ids in (sub.get("affects") or {}).items():
                bucket = merged.setdefault(key, [])
                for i in ids if isinstance(ids, list) else [ids]:
                    if i not in bucket:
                        bucket.append(i)
        return merged

    # -- helpers --------------------------------------------------------------------

    def _matching_subscriptions(self, event: Event) -> list[dict[str, Any]]:
        subs = self.store.subscriptions(event.kind)
        payload = {k: v for k, v in event.payload.items() if k != ACK_KEY}
        return [
            s
            for s in subs
            if s["event_kind"] in (event.kind, "*") and filter_matches(s.get("filter"), payload)
        ]

    @staticmethod
    def _acks(ev: Event) -> list[str]:
        acks = ev.payload.get(ACK_KEY)
        return list(acks) if isinstance(acks, list) else []
