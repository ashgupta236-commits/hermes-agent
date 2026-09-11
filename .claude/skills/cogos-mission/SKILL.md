---
name: cogos-mission
description: How to compile, run, observe, correct, and unblock cogos missions from a high-level objective — use when the human gives an objective, correction, new information, or an authorization.
---

# Operating a cogos mission

## Compile and run
```bash
.venv/bin/python -m cogos mission new "Research whether this business should enter Saudi Arabia." --run
.venv/bin/python -m cogos mission new "Build the feature described in REQUIREMENTS.md." --run --max-cycles 60
.venv/bin/python -m cogos run <mission_id> --max-cycles 40     # continue an existing mission
.venv/bin/python -m cogos --adapter scripted demo               # offline end-to-end demonstration
```
The compiler turns the objective into criteria, constraints, unknowns (with decision value),
hypotheses, a goal hierarchy and a task DAG. It records assumptions instead of asking whenever an
ambiguity has a reasonable default.

## Human interaction (the only inputs a mission needs)
- Objective: `mission new`
- Correction: `python -m cogos correct <id> "<text>"`
- New information: `python -m cogos correct <id> "<text>" --kind information`
- Authorization: `python -m cogos authorize <id> destructive|financial|legally_significant|credential_sensitive|consequential_shared`
- Answer a request: `python -m cogos answer <id> <request_id> "<answer>" [--grant <class>]`
Then `python -m cogos run <id>` — events reactivate blocked/paused missions automatically.

## Observe
- `status [id]` — criteria, task counts, unknowns, blocked operations, human requests, usage
- `trace [id] [--kind select|tool_call|verify|failure|decision|specialist]` — timeline
- `explain [id]` — mission, beliefs, decisions and rationales, tools, specialists, failures,
  retries, cost, unresolved uncertainties
- `workspace [id]` — the compact Global Workspace the executive sees

## Persistence
- Every cycle is saved; snapshots every 5 cycles and at every stop (`.cogos/snapshots/`).
- `checkpoint [id]` / `import <snapshot> --overwrite` for manual recovery.

## Statuses
`draft → active → complete | blocked_external | paused | failed`. `complete` requires the
completion gate: all criteria verified, latest tests passing, no serious unresolved
contradiction, no blocked operation on unfinished tasks, required artifacts verified.
