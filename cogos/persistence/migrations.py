"""Versioned schema migrations for the SQLite state store.

Each migration is idempotent-by-version: the store records the highest applied
version in ``schema_version`` and applies the remaining ones in order inside a
transaction. Add new migrations at the end; never edit an applied one.
"""

from __future__ import annotations

import sqlite3
from typing import Callable

Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]


def _v1(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS missions (
            mission_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            objective TEXT NOT NULL,
            state_json TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_missions_status ON missions(status);

        CREATE TABLE IF NOT EXISTS mission_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            mission_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            ts TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_mission_events_mission ON mission_events(mission_id, seq);

        CREATE TABLE IF NOT EXISTS traces (
            id TEXT PRIMARY KEY,
            mission_id TEXT,
            cycle INTEGER NOT NULL DEFAULT 0,
            kind TEXT NOT NULL,
            summary TEXT NOT NULL,
            data_json TEXT NOT NULL,
            cost_json TEXT NOT NULL,
            ts TEXT NOT NULL,
            parent_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_traces_mission ON traces(mission_id, ts);

        CREATE TABLE IF NOT EXISTS decisions (
            decision_id TEXT PRIMARY KEY,
            mission_id TEXT,
            domain TEXT NOT NULL DEFAULT 'general',
            confidence REAL NOT NULL,
            consequential INTEGER NOT NULL DEFAULT 0,
            outcome_success INTEGER,
            data_json TEXT NOT NULL,
            ts TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_decisions_mission ON decisions(mission_id);

        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            memory_class TEXT NOT NULL,
            mission_id TEXT,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            tags TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL,
            importance REAL NOT NULL,
            valid_from TEXT,
            valid_to TEXT,
            expires_at TEXT,
            superseded_by TEXT,
            version INTEGER NOT NULL DEFAULT 1,
            access_count INTEGER NOT NULL DEFAULT 0,
            last_accessed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            data_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_memories_class ON memories(memory_class);
        CREATE INDEX IF NOT EXISTS idx_memories_hash ON memories(content_hash);
        CREATE INDEX IF NOT EXISTS idx_memories_mission ON memories(mission_id);

        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            source TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            handled INTEGER NOT NULL DEFAULT 0,
            handled_at TEXT,
            data_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_handled ON events(handled, occurred_at);

        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mission_id TEXT NOT NULL,
            event_kind TEXT NOT NULL,
            filter_json TEXT NOT NULL DEFAULT '{}',
            affects_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_subscriptions_kind ON subscriptions(event_kind);

        CREATE TABLE IF NOT EXISTS calibration (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mission_id TEXT,
            domain TEXT NOT NULL,
            predicted REAL NOT NULL,
            outcome INTEGER NOT NULL,
            ref_id TEXT,
            ts TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_calibration_domain ON calibration(domain);

        CREATE TABLE IF NOT EXISTS skills (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            data_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS kv (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(id UNINDEXED, content, tags)"
        )
    except sqlite3.OperationalError:
        # FTS5 unavailable in this SQLite build; retrieval falls back to LIKE scans.
        pass


MIGRATIONS: list[Migration] = [
    (1, "initial schema", _v1),
]


def apply_migrations(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = int(row[0]) if row and row[0] is not None else 0
    for version, _name, fn in MIGRATIONS:
        if version > current:
            fn(conn)
            conn.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
            current = version
    conn.commit()
    return current
