# State, persistence and recovery

All durable state lives in one SQLite database plus JSON snapshots under `COGOS_HOME`
(default `<repo>/.cogos`, git-ignored). Conversation history is never stored; `MissionState` is
the only source of truth.

## Layout

```
$COGOS_HOME/
  cogos.db          # SQLite, WAL mode, busy_timeout 5000 ms, foreign_keys ON
  snapshots/        # <mission_id>.json checkpoints (export_snapshot)
  artifacts/        # reserved for mission artifacts
  skills/           # promoted SKILL.md files written by SkillCompiler
  traces.jsonl      # append-only mirror of every TraceEvent
```

`CogosConfig.ensure_dirs` creates these on `Runtime` construction.

## Schema (`cogos/persistence/migrations.py`, version 1)

| Table | Columns | Used for |
|---|---|---|
| `schema_version` | `version` | highest applied migration |
| `missions` | `mission_id` PK, `status`, `objective`, `state_json`, `version`, `created_at`, `updated_at`; index on `status` | the full `MissionState` aggregate as JSON |
| `mission_events` | `seq` autoincrement PK, `mission_id`, `kind`, `payload_json`, `ts`; index `(mission_id, seq)` | append-only log of every save |
| `traces` | `id` PK, `mission_id`, `cycle`, `kind`, `summary`, `data_json`, `cost_json`, `ts`, `parent_id`; index `(mission_id, ts)` | `TraceEvent`s |
| `decisions` | `decision_id` PK, `mission_id`, `domain`, `confidence`, `consequential`, `outcome_success`, `data_json`, `ts`; index on `mission_id` | decision journal |
| `memories` | `id` PK, `memory_class`, `mission_id`, `content`, `content_hash`, `tags`, `confidence`, `importance`, `valid_from`, `valid_to`, `expires_at`, `superseded_by`, `version`, `access_count`, `last_accessed_at`, `created_at`, `updated_at`, `data_json`; indexes on class, hash, mission | long-term memory |
| `memories_fts` | FTS5 virtual table `(id UNINDEXED, content, tags)` | full-text candidates; created only if the SQLite build has FTS5 |
| `events` | `id` PK, `kind`, `source`, `payload_json`, `occurred_at`, `handled`, `handled_at`, `data_json`; index `(handled, occurred_at)` | event bus |
| `subscriptions` | `id` autoincrement, `mission_id`, `event_kind`, `filter_json`, `affects_json`, `created_at`; index on `event_kind` | event routing |
| `calibration` | `id`, `mission_id`, `domain`, `predicted`, `outcome`, `ref_id`, `ts`; index on `domain` | confidence vs outcome samples |
| `skills` | `id` PK, `name` UNIQUE, `status`, `data_json`, `created_at`, `updated_at` | promoted/rejected skills |
| `kv` | `key` PK, `value_json`, `updated_at` | `last_mission_id`, `skill_signatures` |

### Migration policy

`MIGRATIONS: list[Migration] = [(1, "initial schema", _v1)]`. `apply_migrations` creates
`schema_version`, reads `MAX(version)`, and applies each later migration in order, inserting its
version after it runs, then commits. The rule from the module docstring: "Add new migrations at
the end; never edit an applied one." `StateStore.schema_version` and `health()` report the current
version; snapshots record it too. `MissionState.schema_version` (pydantic, currently `1`) is
separate from the database version.

## Optimistic concurrency

`StateStore.save_mission(state, event_kind, payload)`:

1. `state.touch()` updates `timestamps.updated_at`.
2. Reads the stored `version` for the mission.
3. `BEGIN IMMEDIATE`; on first insert sets `state.version = 1`; otherwise, if the stored version
   differs from `state.version`, raises `StoreConflict` and rolls back; else increments
   `state.version` and updates the row.
4. Inserts a `mission_events` row (`payload` defaults to `{"version": n}`) and commits.

`load_mission` returns the deserialised state with `version` set from the row. Any holder of a
stale in-memory copy therefore fails on save rather than clobbering newer state. `import_snapshot`
resets `version` to 0 before saving so the imported mission starts at version 1.

## Event log

`mission_events.kind` values written by the code: `mission_compiled`, `run_started`, `cycle`
(payload: cycle number, operation, stop, reason, status), `checkpoint`, `human_input`,
`authorized`, `reactivated`, `snapshot_imported`, `skill_candidate`, `skill_evaluated`, and
`tasks_added` (evaluation scenarios); the default is `state_saved`. `StateStore.mission_events(mid,
since_seq)` returns them in order. The log records every save, not a diff; the state itself is in
`missions.state_json`.

## Traces

`Tracer.emit` writes a `TraceEvent` to `traces` (and to `traces.jsonl`, and to stdout with `-v`).
Kinds emitted by the runtime: `mission_compiled`, `cycle_start`, `assess`, `select`, `operation`,
`tool_call`, `specialist`, `verify`, `decision`, `failure`, `retry`, `blocked`, `event`, `learn`,
`checkpoint`, `complete`, `residency`, `resume`, `error`. `Tracer.explain(mission_id)` answers the
observability questions from traces alone; `Tracer.timeline` renders them.

## Snapshots

`StateStore.export_snapshot(mission_id, directory)` writes `<directory>/<mission_id>.json`
atomically (temp file then rename):

```json
{
  "format": "cogos-snapshot",
  "format_version": 1,
  "exported_at": "...",
  "schema_version": 1,
  "mission": { ...MissionState... },
  "decisions": [ ... ],
  "traces": [ ... up to 5000 ... ],
  "events": [ ...mission_events... ]
}
```

`import_snapshot(path, overwrite=False)` validates `format`, refuses to overwrite an existing
mission unless `overwrite=True` (deleting the old mission, its events and traces first), saves the
mission with event kind `snapshot_imported`, and re-records decisions and traces. Memories,
calibration samples, event-bus events and subscriptions are not part of a snapshot.

CLI: `python -m cogos checkpoint [mission_id]` and `python -m cogos import <file> [--overwrite]`.

## Checkpoint cadence

* `Executive._persist` runs after **every** cycle: `save_mission(state, "cycle", payload)`.
* `_persist` also calls `_checkpoint` when `usage.cycles % 5 == 0` or the cycle set `stop`.
* `Executive.run` calls `_checkpoint(final=True)` when the loop exits.
* `_checkpoint` exports a snapshot, sets `timestamps.last_checkpoint_at`, traces `checkpoint`, then
  saves the mission again with event kind `checkpoint`. Checkpoint failures are traced as `error`
  and never abort the run.

`CogosConfig.checkpoint_every_cycles` (default 1) exists but is not read by the loop; the cadence
above is fixed in code.

## Boot protocol

`Runtime.boot(brief=False)` returns a `BootReport`:

| Field | Source |
|---|---|
| `store_health` | `StateStore.health()`: path, schema version, `PRAGMA quick_check`, FTS flag, row counts |
| `git_status`, `git_log` | `git status --short -b`, `git log --oneline -5` |
| `environment` | executive model, adapter, available/unavailable tools, `claude_binary` path (warns if missing) |
| `missions` | every mission with status and objective |
| `resume_target`, `resume_reason` | `Runtime.resume_target()` |
| `pending_events` | unhandled bus events and their routes |
| `unresolved_tasks`, `latest_verification`, `human_requests` | from the resume target's state |
| `workspace` | `GlobalWorkspace` digest of the resume target (omitted with `--brief`) |
| `warnings` | adapter unavailable, store integrity not `ok`, git unavailable |

`resume_target` ranks missions:

1. `active`
2. `paused` or `blocked_external` **with pending events** (`EventBus.wake_targets`)
3. `draft` (compiled but never started)
4. `paused` without events

`complete`, `failed`, `abandoned`, and `blocked_external` without pending events are never
auto-resumed. `cogos run` / `cogos resume` with no argument use this ranking; with an explicit id
the reason is `explicit`.

## What survives a context restart

Everything the loop needs is in the database: the whole `MissionState` (criteria, constraints,
facts, assumptions, unknowns, hypotheses, goals, tasks with attempts and failure signatures,
evidence, claims, contradictions, decisions, commitments, risks, blocked operations, artifacts,
tests, lessons, candidate skills, human requests, world model, confidence, progress,
`executive_model`, `capability_state`, `permission_state`, usage counters, budget, timestamps,
synthesis, notes, `resources.controller` history), plus traces, decisions, memories, events,
subscriptions, calibration samples, skills and `kv.last_mission_id`.

Held only in process memory and rebuilt on start: `Executive._tool_history` (recent tool
success/failure window), `ToolFabric.call_log`, `CapabilityFirewall.audit` and `human_grants`
(re-derived from `permissions.grants` by `_apply_grants` at the start of every run),
`AgentFoundry.spawned`, and the tracer's per-process sequence counter.

### How scenario E measures it

`E_context_restart` (`cogos/evaluation/scenarios.py`) runs three cycles, dumps the mission JSON,
closes the runtime, opens a fresh `Runtime` over the same `COGOS_HOME` with a new scripted adapter
and no prose summary, calls `boot()`, reloads the mission and compares every top-level field:

```python
fields_equal = sum(1 for k, v in snapshot_before.items() if json.loads(reloaded.model_dump_json()).get(k) == v)
recovery_accuracy = fields_equal / len(snapshot_before)
```

It asserts `boot_found_mission` (resume target is the mission), `state_reconstructed_exactly`
(accuracy >= 0.99), `resumed_without_human`, `did_not_redo_completed_tasks` (no completed task
gains attempts), `cycle_counter_continued`, and `completed_after_restart`. The
`context_recovery_accuracy` metric in EVALS.md is that ratio. `adv_interrupted_run` covers the
harder case of a `KeyboardInterrupt` mid-cycle and checks `PRAGMA quick_check == "ok"` afterwards.

## `COGOS_HOME`

`load_config` sets `home` from (in order) `cogos.yaml` `home`, the `COGOS_HOME` environment
variable, or `<repo>/.cogos`; a relative path is resolved against `repo_root`. The `--home` CLI
flag overrides all of them. `COGOS_HOME` is also added to the firewall's writable roots so the
runtime can always write its own state.
