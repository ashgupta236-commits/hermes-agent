"""Event sources: plain, poll-driven producers of :class:`Event` objects.

No threads, no sockets. The scheduler polls each source and pushes whatever
it returns onto the :class:`EventBus`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from cogos.ids import iso_now
from cogos.schemas.events import Event
from cogos.schemas.tools import ToolResult


def _parse_ts(value: Optional[str]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _file_fingerprint(path: Path, hash_limit_bytes: int = 4_000_000) -> Optional[dict[str, Any]]:
    try:
        st = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    fp: dict[str, Any] = {"mtime": st.st_mtime, "size": st.st_size}
    if st.st_size <= hash_limit_bytes:
        h = hashlib.sha256()
        try:
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
            fp["sha256"] = h.hexdigest()
        except OSError:
            pass
    return fp


class FileWatchSource:
    """Detects created/modified/deleted files under the watched paths."""

    def __init__(self, paths: list[Path], state_path: Optional[Path] = None, mission_ids: Optional[list[str]] = None):
        self.paths = [Path(p) for p in paths]
        self.state_path = Path(state_path) if state_path is not None else None
        self.mission_ids = list(mission_ids or [])
        self._snapshot: dict[str, dict[str, Any]] = self._load_snapshot()
        self._primed = bool(self._snapshot) or self.state_path is not None and self.state_path.exists()
        if not self._primed:
            self._snapshot = self._scan()
            self._primed = True
            self._save_snapshot()

    def _load_snapshot(self) -> dict[str, dict[str, Any]]:
        if self.state_path is None or not self.state_path.exists():
            return {}
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_snapshot(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as fh:
            json.dump(self._snapshot, fh, sort_keys=True)

    def _iter_files(self):
        for root in self.paths:
            if root.is_file():
                yield root
            elif root.is_dir():
                for p in sorted(root.rglob("*")):
                    if p.is_file():
                        yield p

    def _scan(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for p in self._iter_files():
            fp = _file_fingerprint(p)
            if fp is not None:
                out[str(p)] = fp
        return out

    @staticmethod
    def _changed(old: dict[str, Any], new: dict[str, Any]) -> bool:
        if "sha256" in old and "sha256" in new:
            return old["sha256"] != new["sha256"]
        return old.get("mtime") != new.get("mtime") or old.get("size") != new.get("size")

    def poll(self) -> list[Event]:
        current = self._scan()
        events: list[Event] = []
        for path, fp in current.items():
            old = self._snapshot.get(path)
            if old is None:
                change = "created"
            elif self._changed(old, fp):
                change = "modified"
            else:
                continue
            events.append(self._event(path, change, fp))
        for path in self._snapshot:
            if path not in current:
                events.append(self._event(path, "deleted", {}))
        self._snapshot = current
        self._save_snapshot()
        return events

    def _event(self, path: str, change: str, fp: dict[str, Any]) -> Event:
        return Event(
            kind="file_changed",
            source="file_watch",
            payload={"path": path, "change": change, "size": fp.get("size"), "sha256": fp.get("sha256")},
            mission_ids=list(self.mission_ids),
            trusted=False,
        )


class DeadlineSource:
    """Fires ``deadline`` events once a mission's deadline has passed."""

    def __init__(self, deadlines: list[tuple[str, str]]):
        self.deadlines: list[tuple[str, str]] = [(m, ts) for m, ts in deadlines]
        self._fired: set[tuple[str, str]] = set()

    def add(self, mission_id: str, deadline_iso: str) -> None:
        self.deadlines.append((mission_id, deadline_iso))

    def poll(self, now: Optional[str] = None) -> list[Event]:
        current = _parse_ts(now)
        events: list[Event] = []
        for mission_id, ts in self.deadlines:
            key = (mission_id, ts)
            if key in self._fired:
                continue
            if _parse_ts(ts) <= current:
                self._fired.add(key)
                events.append(
                    Event(
                        kind="deadline",
                        source="system",
                        payload={"deadline": ts, "now": current.isoformat(timespec="milliseconds")},
                        mission_ids=[mission_id],
                        trusted=True,
                    )
                )
        return events


class ScheduledSource:
    """Fires a ``scheduled`` event every ``interval_seconds``."""

    def __init__(self, interval_seconds: float, last_fire_at: Optional[str] = None, mission_ids: Optional[list[str]] = None, name: str = "tick"):
        self.interval_seconds = float(interval_seconds)
        self.last_fire_at = last_fire_at
        self.mission_ids = list(mission_ids or [])
        self.name = name

    def poll(self, now: Optional[str] = None) -> list[Event]:
        current = _parse_ts(now)
        if self.last_fire_at is not None:
            elapsed = (current - _parse_ts(self.last_fire_at)).total_seconds()
            if elapsed < self.interval_seconds:
                return []
        self.last_fire_at = current.isoformat(timespec="milliseconds")
        return [
            Event(
                kind="scheduled",
                source="system",
                payload={"name": self.name, "interval_seconds": self.interval_seconds, "fired_at": self.last_fire_at},
                mission_ids=list(self.mission_ids),
                trusted=True,
            )
        ]


class ExternalJobSource:
    """Completion of long-running external jobs (CI runs, batch jobs, remote agents)."""

    def __init__(self, source_name: str = "external_job"):
        self.source_name = source_name
        self.completed: list[Event] = []

    def complete(self, job_id: str, mission_id: str, result: dict[str, Any]) -> Event:
        ev = Event(
            kind="job_completed",
            source=self.source_name,
            payload={"job_id": job_id, "result": dict(result), "completed_at": iso_now()},
            mission_ids=[mission_id],
            trusted=False,
        )
        self.completed.append(ev)
        return ev

    def poll(self) -> list[Event]:
        out, self.completed = self.completed, []
        return out


class TestCompletionSource:
    """Turns a test-runner :class:`ToolResult` into a ``test_completed`` event."""

    @staticmethod
    def from_tool_result(mission_id: str, tool_result: ToolResult) -> Event:
        data = tool_result.data or {}
        counts = {
            k: data[k]
            for k in ("passed", "failed", "errors", "skipped", "total")
            if isinstance(data.get(k), int)
        }
        if not counts and isinstance(data.get("counts"), dict):
            counts = dict(data["counts"])
        summary = data.get("summary") or tool_result.error or _first_line(tool_result.output)
        return Event(
            kind="test_completed",
            source="tests",
            payload={"ok": bool(tool_result.ok), "summary": summary, "counts": counts, "tool": tool_result.tool, "call_id": tool_result.call_id},
            mission_ids=[mission_id],
            trusted=False,
        )


def _first_line(text: str, limit: int = 300) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""
