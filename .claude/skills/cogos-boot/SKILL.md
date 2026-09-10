---
name: cogos-boot
description: Boot/recovery protocol for a fresh Claude Code context in this repository — reconstruct cogos mission state from durable storage and resume the highest-priority unfinished mission without asking the human to restate anything.
---

# cogos boot / recovery protocol

Run this at the start of any session that touches autonomous missions, after a context reset, or
whenever you are unsure what state the runtime is in.

## Procedure
1. `cd` to the repository root (the directory containing `CLAUDE.md` and `cogos/`).
2. `.venv/bin/python -m cogos boot` — prints: store health, mission registry, resume target and
   reason, unresolved tasks, latest verification records, pending events, unanswered human
   requests, git status/log, environment (executive model, adapter, available tools), warnings,
   and the reconstructed Global Workspace of the resume target.
3. `git status --short -b` and `git log --oneline -10` — confirm code state matches the mission's
   recorded artifacts/tests.
4. If the boot report lists **awaiting human** items, present them verbatim to the human and
   continue with every independent task in the meantime (`python -m cogos run <id>`).
5. If the store integrity is not `ok`, import the latest snapshot:
   `python -m cogos import .cogos/snapshots/<mission>.json --overwrite`.
6. Resume: `.venv/bin/python -m cogos resume` (or `run <mission_id> --max-cycles N`).
7. Observe: `python -m cogos status`, `trace`, `explain <id>`, `workspace <id>`.

## Rules
- Do not rely on any prose summary generated at compaction; the filesystem/state store is the
  reconstruction source.
- Do not ask the human what happened; the trace answers it (`python -m cogos explain <id>`).
- A `BLOCKED_EXTERNAL` mission lists exactly what would unblock it; a `PAUSED` mission is waiting
  for an event or a budget decision (`cogos.yaml: budget`).
