# cogos documentation

`cogos` is a persistent, high-autonomy cognitive operating system built around a resident
Claude executive model. It lives in `cogos/` at the repository root, ships its own CLI
(`python -m cogos`) and tests (`tests/cogos/`), and touches no Hermes core file.

| Document | What it covers |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | The Python-owned executive cycle, module map, cognition call sites and their structured-output contracts, adapters, deterministic boundaries, deviations from the plan. |
| [AUTONOMY.md](AUTONOMY.md) | What the runtime decides itself, the few cases that require a human, `BLOCKED_EXTERNAL` semantics, retry/replan policy, the completion gate. |
| [MODEL_POLICY.md](MODEL_POLICY.md) | Resident model (`claude-fable-5-1`), residency verification, no-downgrade rule, specialist model inheritance, Claude Code CLI flags. |
| [STATE.md](STATE.md) | SQLite schema, migrations, optimistic concurrency, event log, snapshots, checkpoint cadence, boot/resume protocol, what survives a restart. |
| [MEMORY.md](MEMORY.md) | Nine memory classes, selective writes, dedupe, contradiction detection, versioning, retrieval scoring, consolidation, quarantine. |
| [EVALS.md](EVALS.md) | The twelve metrics, acceptance scenarios A-J, adversarial scenarios, how to run, measured results. |
| [SECURITY.md](SECURITY.md) | Capability firewall, injection defence, trust levels, false-consensus guard, specialist tool surface, non-goals. |
| [EXAMPLE_MISSION.md](EXAMPLE_MISSION.md) | A research mission walked end to end: what the human supplies, what the system decides, how completion is earned. |
| [evidence/INCIDENT_REPAIR.md](evidence/INCIDENT_REPAIR.md) | The repair of the evidence pipeline after Live Run #2: confirmed root causes for F1-F3 with code references, eight further defects found while repairing (including a false completion), what was deliberately rejected, measured before/after, and the weaknesses that remain. |
| [evidence/LIVE_RUN_3_PROTOCOL.md](evidence/LIVE_RUN_3_PROTOCOL.md) | The protocol for Live Run #3 — **prepared, not executed**: freeze, pre-run gate, workspace, budget, instrumentation, outcome classification and failure controls. |
| [evidence/LIVE_RUN_2.md](evidence/LIVE_RUN_2.md) | Live Run #2, the controlled validation of the repaired controller against the real adapter: hypotheses H1-H8 scored one by one, the first bound receipt under a frontier model, admission control stopping under budget, three new defects, and why the verdict is *partial* rather than strong. |
| [evidence/LIVE_RUN_FOLLOWUP.md](evidence/LIVE_RUN_FOLLOWUP.md) | The incident review: root cause of the live failure, the repairs, before-and-after on the same inputs, remaining weaknesses, and whether another live run is justified. |
| [evidence/LIVE_RUN.md](evidence/LIVE_RUN.md) | The live `claude_code` run: what it validated (residency, completion integrity, resume, budget guards), what it did not (the reality anchor never ran), the two defects it found, and its cost. |
| [UPGRADE_STATUS.md](UPGRADE_STATUS.md) | F1-F7 repairs, R1-R5 and L1-L6 additions, the A01-A20 acceptance matrix with implemented / verified-locally / validated-live tracked separately, and what the evidence does not show. |
| [OPERATIONS.md](OPERATIONS.md) | Setup, every CLI command, `cogos.yaml`, environment variables, human answer/authorize flow, snapshots, troubleshooting. |

## Quickstart

```bash
cd /home/user/hermes-agent
uv sync --extra dev                                   # Python >=3.11; installs pytest
.venv/bin/python -m cogos demo                        # offline end-to-end run (~5 s, scripted adapter)
.venv/bin/python -m cogos init                        # writes cogos.yaml, creates .cogos/cogos.db
.venv/bin/python -m cogos mission new "Build the feature described in REQUIREMENTS.md." --run
.venv/bin/python -m cogos status                      # last mission: progress, criteria, blocked ops, human requests
.venv/bin/python -m cogos boot                        # after a restart: reconstruct state, pick the resume target
.venv/bin/python -m cogos resume                      # continue the highest-priority unfinished mission
.venv/bin/python -m cogos eval --suite all --json     # acceptance + adversarial scenarios, offline
.venv/bin/python -m pytest -q tests/cogos             # unit tests
```

The default adapter is `claude_code` (headless `claude -p`, model pinned to `claude-fable-5-1`).
`demo` and `eval` always use the deterministic `scripted` adapter and need no network or API key.
