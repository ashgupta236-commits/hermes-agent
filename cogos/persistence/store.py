"""SQLite-backed durable state store.

Design:
* ``missions`` holds the full :class:`MissionState` aggregate as JSON with an
  optimistic-concurrency ``version``; writes are atomic and every write also
  appends a ``mission_events`` row so the history can be replayed/audited.
* Cross-mission knowledge (memories, decisions, calibration, skills, events)
  lives in normalised tables so it can be queried without loading missions.
* Snapshots export a mission (state + traces + decisions) to a JSON file for
  checkpoints and for reconstruction after catastrophic DB loss.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

from cogos.ids import iso_now
from cogos.persistence.migrations import apply_migrations
from cogos.schemas.decisions import Decision
from cogos.schemas.events import Event
from cogos.schemas.memory import MemoryClass, MemoryRecord
from cogos.schemas.mission import MissionState, MissionStatus
from cogos.schemas.trace import TraceEvent


class StoreConflict(RuntimeError):
    """Raised when a mission was modified concurrently (version mismatch)."""


def content_hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.lower().split()).encode("utf-8")).hexdigest()[:24]


class StateStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.schema_version = apply_migrations(self._conn)
        self._fts = self._has_fts()

    # -- lifecycle -------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _has_fts(self) -> bool:
        row = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memories_fts'"
        ).fetchone()
        return row is not None

    # -- missions --------------------------------------------------------------

    def save_mission(self, state: MissionState, event_kind: str = "state_saved", payload: Optional[dict[str, Any]] = None) -> MissionState:
        """Atomically persist ``state``; bumps ``version`` and appends an event."""
        with self._lock:
            state.touch()
            row = self._conn.execute(
                "SELECT version FROM missions WHERE mission_id=?", (state.mission_id,)
            ).fetchone()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if row is None:
                    state.version = 1
                    self._conn.execute(
                        "INSERT INTO missions(mission_id,status,objective,state_json,version,created_at,updated_at)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (
                            state.mission_id,
                            state.status.value,
                            state.objective,
                            state.model_dump_json(),
                            state.version,
                            state.timestamps.created_at,
                            state.timestamps.updated_at,
                        ),
                    )
                else:
                    if int(row["version"]) != state.version:
                        raise StoreConflict(
                            f"mission {state.mission_id}: stored version {row['version']} != in-memory {state.version}"
                        )
                    state.version += 1
                    self._conn.execute(
                        "UPDATE missions SET status=?, objective=?, state_json=?, version=?, updated_at=? WHERE mission_id=?",
                        (
                            state.status.value,
                            state.objective,
                            state.model_dump_json(),
                            state.version,
                            state.timestamps.updated_at,
                            state.mission_id,
                        ),
                    )
                self._conn.execute(
                    "INSERT INTO mission_events(mission_id,kind,payload_json,ts) VALUES (?,?,?,?)",
                    (state.mission_id, event_kind, json.dumps(payload or {"version": state.version}), iso_now()),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return state

    def load_mission(self, mission_id: str) -> Optional[MissionState]:
        with self._lock:
            row = self._conn.execute(
                "SELECT state_json, version FROM missions WHERE mission_id=?", (mission_id,)
            ).fetchone()
        if row is None:
            return None
        state = MissionState.model_validate_json(row["state_json"])
        state.version = int(row["version"])
        return state

    def list_missions(self, status: Optional[MissionStatus] = None) -> list[dict[str, Any]]:
        q = "SELECT mission_id, status, objective, version, created_at, updated_at FROM missions"
        args: tuple[Any, ...] = ()
        if status is not None:
            q += " WHERE status=?"
            args = (status.value,)
        q += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def delete_mission(self, mission_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM missions WHERE mission_id=?", (mission_id,))
            self._conn.execute("DELETE FROM mission_events WHERE mission_id=?", (mission_id,))
            self._conn.execute("DELETE FROM traces WHERE mission_id=?", (mission_id,))

    def mission_events(self, mission_id: str, since_seq: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, kind, payload_json, ts FROM mission_events WHERE mission_id=? AND seq>? ORDER BY seq",
                (mission_id, since_seq),
            ).fetchall()
        return [
            {"seq": r["seq"], "kind": r["kind"], "payload": json.loads(r["payload_json"]), "ts": r["ts"]}
            for r in rows
        ]

    # -- traces ----------------------------------------------------------------

    def record_trace(self, ev: TraceEvent) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO traces(id,mission_id,cycle,kind,summary,data_json,cost_json,ts,parent_id)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    ev.id,
                    ev.mission_id,
                    ev.cycle,
                    ev.kind,
                    ev.summary,
                    json.dumps(ev.data, default=str),
                    json.dumps(ev.cost, default=str),
                    ev.ts,
                    ev.parent_id,
                ),
            )

    def traces(self, mission_id: Optional[str] = None, kind: Optional[str] = None, limit: int = 500) -> list[TraceEvent]:
        q = "SELECT * FROM traces"
        clauses, args = [], []
        if mission_id:
            clauses.append("mission_id=?")
            args.append(mission_id)
        if kind:
            clauses.append("kind=?")
            args.append(kind)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY ts, id LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [
            TraceEvent(
                id=r["id"],
                mission_id=r["mission_id"],
                cycle=r["cycle"],
                kind=r["kind"],
                summary=r["summary"],
                data=json.loads(r["data_json"]),
                cost=json.loads(r["cost_json"]),
                ts=r["ts"],
                parent_id=r["parent_id"],
            )
            for r in rows
        ]

    # -- decisions / calibration -------------------------------------------------

    def record_decision(self, mission_id: Optional[str], decision: Decision) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO decisions(decision_id,mission_id,domain,confidence,consequential,outcome_success,data_json,ts)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    decision.decision_id,
                    mission_id,
                    decision.domain,
                    decision.confidence,
                    int(decision.consequential),
                    None if decision.outcome_success is None else int(decision.outcome_success),
                    decision.model_dump_json(),
                    decision.timestamp,
                ),
            )

    def decisions(self, mission_id: Optional[str] = None) -> list[Decision]:
        q = "SELECT data_json FROM decisions"
        args: tuple[Any, ...] = ()
        if mission_id:
            q += " WHERE mission_id=?"
            args = (mission_id,)
        q += " ORDER BY ts"
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [Decision.model_validate_json(r["data_json"]) for r in rows]

    def record_calibration(self, mission_id: Optional[str], domain: str, predicted: float, outcome: bool, ref_id: Optional[str] = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO calibration(mission_id,domain,predicted,outcome,ref_id,ts) VALUES (?,?,?,?,?,?)",
                (mission_id, domain, float(predicted), int(bool(outcome)), ref_id, iso_now()),
            )

    def calibration_samples(self, domain: Optional[str] = None) -> list[tuple[float, bool]]:
        q = "SELECT predicted, outcome FROM calibration"
        args: tuple[Any, ...] = ()
        if domain:
            q += " WHERE domain=?"
            args = (domain,)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [(float(r["predicted"]), bool(r["outcome"])) for r in rows]

    # -- memories ------------------------------------------------------------------

    def put_memory(self, rec: MemoryRecord) -> MemoryRecord:
        rec.content_hash = rec.content_hash or content_hash(rec.content)
        rec.updated_at = iso_now()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO memories(id,memory_class,mission_id,content,content_hash,tags,confidence,importance,"
                "valid_from,valid_to,expires_at,superseded_by,version,access_count,last_accessed_at,created_at,updated_at,data_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rec.id,
                    rec.memory_class.value,
                    rec.mission_id,
                    rec.content,
                    rec.content_hash,
                    ",".join(rec.tags),
                    rec.confidence,
                    rec.importance,
                    rec.valid_from,
                    rec.valid_to,
                    rec.expires_at,
                    rec.superseded_by,
                    rec.version,
                    rec.access_count,
                    rec.last_accessed_at,
                    rec.created_at,
                    rec.updated_at,
                    rec.model_dump_json(),
                ),
            )
            if self._fts:
                self._conn.execute("DELETE FROM memories_fts WHERE id=?", (rec.id,))
                self._conn.execute(
                    "INSERT INTO memories_fts(id,content,tags) VALUES (?,?,?)",
                    (rec.id, rec.content, " ".join(rec.tags)),
                )
        return rec

    def get_memory(self, memory_id: str) -> Optional[MemoryRecord]:
        with self._lock:
            row = self._conn.execute("SELECT data_json FROM memories WHERE id=?", (memory_id,)).fetchone()
        return MemoryRecord.model_validate_json(row["data_json"]) if row else None

    def find_memory_by_hash(self, h: str, memory_class: Optional[MemoryClass] = None) -> Optional[MemoryRecord]:
        q = "SELECT data_json FROM memories WHERE content_hash=? AND superseded_by IS NULL"
        args: list[Any] = [h]
        if memory_class:
            q += " AND memory_class=?"
            args.append(memory_class.value)
        with self._lock:
            row = self._conn.execute(q, args).fetchone()
        return MemoryRecord.model_validate_json(row["data_json"]) if row else None

    def all_memories(self, memory_class: Optional[MemoryClass] = None, mission_id: Optional[str] = None, include_superseded: bool = False) -> list[MemoryRecord]:
        q = "SELECT data_json FROM memories"
        clauses, args = [], []
        if memory_class:
            clauses.append("memory_class=?")
            args.append(memory_class.value)
        if mission_id:
            clauses.append("mission_id=?")
            args.append(mission_id)
        if not include_superseded:
            clauses.append("superseded_by IS NULL")
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY importance DESC, updated_at DESC"
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [MemoryRecord.model_validate_json(r["data_json"]) for r in rows]

    def search_memory_text(self, query: str, limit: int = 50) -> list[MemoryRecord]:
        """Full-text candidates (FTS5 when available, LIKE fallback)."""
        terms = [t for t in "".join(ch if ch.isalnum() else " " for ch in query).split() if len(t) > 2]
        if not terms:
            return []
        with self._lock:
            if self._fts:
                match = " OR ".join(f'"{t}"' for t in terms[:20])
                try:
                    rows = self._conn.execute(
                        "SELECT m.data_json FROM memories_fts f JOIN memories m ON m.id=f.id "
                        "WHERE memories_fts MATCH ? AND m.superseded_by IS NULL LIMIT ?",
                        (match, limit),
                    ).fetchall()
                    return [MemoryRecord.model_validate_json(r["data_json"]) for r in rows]
                except sqlite3.OperationalError:
                    pass
            clauses = " OR ".join(["lower(content) LIKE ?"] * len(terms[:20]))
            rows = self._conn.execute(
                f"SELECT data_json FROM memories WHERE superseded_by IS NULL AND ({clauses}) LIMIT ?",
                [f"%{t.lower()}%" for t in terms[:20]] + [limit],
            ).fetchall()
        return [MemoryRecord.model_validate_json(r["data_json"]) for r in rows]

    def delete_memory(self, memory_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            if self._fts:
                self._conn.execute("DELETE FROM memories_fts WHERE id=?", (memory_id,))

    # -- events / subscriptions -------------------------------------------------------

    def put_event(self, ev: Event) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO events(id,kind,source,payload_json,occurred_at,handled,handled_at,data_json)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (ev.id, ev.kind, ev.source, json.dumps(ev.payload, default=str), ev.occurred_at, int(ev.handled), ev.handled_at, ev.model_dump_json()),
            )

    def pending_events(self) -> list[Event]:
        with self._lock:
            rows = self._conn.execute("SELECT data_json FROM events WHERE handled=0 ORDER BY occurred_at").fetchall()
        return [Event.model_validate_json(r["data_json"]) for r in rows]

    def subscribe(self, mission_id: str, event_kind: str, filter_: Optional[dict[str, Any]] = None, affects: Optional[dict[str, Any]] = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO subscriptions(mission_id,event_kind,filter_json,affects_json,created_at) VALUES (?,?,?,?,?)",
                (mission_id, event_kind, json.dumps(filter_ or {}), json.dumps(affects or {}), iso_now()),
            )

    def subscriptions(self, event_kind: Optional[str] = None) -> list[dict[str, Any]]:
        q = "SELECT mission_id, event_kind, filter_json, affects_json FROM subscriptions"
        args: tuple[Any, ...] = ()
        if event_kind:
            q += " WHERE event_kind=? OR event_kind='*'"
            args = (event_kind,)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [
            {"mission_id": r["mission_id"], "event_kind": r["event_kind"], "filter": json.loads(r["filter_json"]), "affects": json.loads(r["affects_json"])}
            for r in rows
        ]

    # -- skills / kv ---------------------------------------------------------------------

    def put_skill(self, skill_id: str, name: str, status: str, data: dict[str, Any]) -> None:
        now = iso_now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO skills(id,name,status,data_json,created_at,updated_at) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(name) DO UPDATE SET status=excluded.status, data_json=excluded.data_json, updated_at=excluded.updated_at",
                (skill_id, name, status, json.dumps(data, default=str), now, now),
            )

    def skills(self, status: Optional[str] = None) -> list[dict[str, Any]]:
        q = "SELECT id,name,status,data_json,created_at,updated_at FROM skills"
        args: tuple[Any, ...] = ()
        if status:
            q += " WHERE status=?"
            args = (status,)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [dict(r) | {"data": json.loads(r["data_json"])} for r in rows]

    def kv_set(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO kv(key,value_json,updated_at) VALUES (?,?,?)",
                (key, json.dumps(value, default=str), iso_now()),
            )

    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value_json FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value_json"]) if row else default

    # -- snapshots -----------------------------------------------------------------------

    def export_snapshot(self, mission_id: str, directory: Path) -> Path:
        state = self.load_mission(mission_id)
        if state is None:
            raise KeyError(mission_id)
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "format": "cogos-snapshot",
            "format_version": 1,
            "exported_at": iso_now(),
            "schema_version": self.schema_version,
            "mission": json.loads(state.model_dump_json()),
            "decisions": [json.loads(d.model_dump_json()) for d in self.decisions(mission_id)],
            "traces": [json.loads(t.model_dump_json()) for t in self.traces(mission_id, limit=5000)],
            "events": self.mission_events(mission_id),
        }
        path = directory / f"{mission_id}.json"
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, default=str)
        tmp.replace(path)
        state.timestamps.last_checkpoint_at = iso_now()
        return path

    def import_snapshot(self, path: Path, overwrite: bool = False) -> MissionState:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        if payload.get("format") != "cogos-snapshot":
            raise ValueError("not a cogos snapshot")
        state = MissionState.model_validate(payload["mission"])
        existing = self.load_mission(state.mission_id)
        if existing is not None and not overwrite:
            raise StoreConflict(f"mission {state.mission_id} already exists")
        if existing is not None:
            self.delete_mission(state.mission_id)
        state.version = 0
        self.save_mission(state, event_kind="snapshot_imported", payload={"path": str(path)})
        for d in payload.get("decisions", []):
            self.record_decision(state.mission_id, Decision.model_validate(d))
        for t in payload.get("traces", []):
            self.record_trace(TraceEvent.model_validate(t))
        return state

    # -- health ---------------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        with self._lock:
            integrity = self._conn.execute("PRAGMA quick_check").fetchone()[0]
            counts = {
                t: self._conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("missions", "mission_events", "traces", "decisions", "memories", "events", "skills")
            }
        return {"path": str(self.path), "schema_version": self.schema_version, "integrity": integrity, "fts": self._fts, "counts": counts}

    def iter_mission_states(self) -> Iterable[MissionState]:
        for row in self.list_missions():
            st = self.load_mission(row["mission_id"])
            if st is not None:
                yield st
