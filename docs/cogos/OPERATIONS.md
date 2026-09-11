# Operations

How to install, start, resume, observe, debug and recover a cogos runtime.

## Setup

```bash
cd /home/user/hermes-agent
uv sync --extra dev                    # Python >=3.11,<3.14; dev extra provides pytest
.venv/bin/python -m cogos --help
.venv/bin/python -m cogos init         # writes cogos.yaml (unless --no-config) and prints store health
```

`init` creates `.cogos/cogos.db` and the `snapshots/`, `artifacts/`, `skills/` directories. The
generated `cogos.yaml`:

```yaml
executive:
  model: claude-fable-5-1
  adapter: claude_code   # claude_code | anthropic_api | scripted
  effort: high
  allow_cheaper_specialist_models: false
governance:
  allow_network: true
  allow_shell: true
  always_require_human: [destructive, financial, legally_significant, credential_sensitive]
budget:
  max_cycles: 200
  max_model_calls: 400
  max_subagents: 20
```

Makefile shortcuts (repository root): `make cogos-setup` (`uv sync --extra dev`), `make cogos-test`
(`pytest tests/cogos -q`), `make cogos-lint` (ruff), `make cogos-typecheck` (ty), `make cogos-eval`
(writes `.cogos/eval-report.json`), `make cogos-demo`, `make cogos-boot`, and `make cogos-check`
(all of the above). Claude Code sessions in this repository also get a `SessionStart` hook
(`.claude/hooks/cogos-boot.sh`) that prints the brief boot report when `.cogos/` exists and a
`PreCompact` hook that exports a checkpoint; `Runtime.new_mission` discovers
`.claude/skills/*/SKILL.md` (alongside `.cogos/skills/`) and offers up to three relevant skill
descriptions to the compiler.

For `claude_code` the `claude` binary must be on `PATH` (`executive.claude_binary` to override).
For `anthropic_api` install the extra (`pip install 'hermes-agent[anthropic]'`) and export
`ANTHROPIC_API_KEY`. `scripted` needs nothing.

## `cogos.yaml` keys

| Key | Default | Meaning |
|---|---|---|
| `home` | `COGOS_HOME` or `<repo>/.cogos` | state directory |
| `repo_root` | git root of cwd | working directory for tools and compilation context |
| `executive.model` | `claude-fable-5-1` | resident executive model |
| `executive.adapter` | `claude_code` | `claude_code`, `anthropic_api`, `scripted` |
| `executive.allow_cheaper_specialist_models` | `false` | permit `specialist_model` |
| `executive.specialist_model` | `null` | model for specialists when allowed |
| `executive.effort` | `high` | `low|medium|high|xhigh|max` |
| `executive.call_timeout_seconds` | `900` | per cognition call |
| `executive.max_retries` | `3` | transient retries in the adapter |
| `executive.claude_binary` | `claude` | CLI path |
| `executive.extra_cli_args` | `[]` | appended to every `claude` invocation |
| `governance.allow_network` | `true` | enables `web_fetch` |
| `governance.allowed_domains` / `denied_domains` | `[]` / `[]` | host allow/deny lists (suffix match) |
| `governance.allow_shell` | `true` | enables `shell` and `run_tests` |
| `governance.shell_timeout_seconds` | `120` | tool timeout |
| `governance.writable_roots` | `[]` (repo root) | where `write_file`/`delete_file` are local |
| `governance.always_require_human` | `[destructive, financial, legally_significant, credential_sensitive]` | classes needing a grant |
| `governance.denied_action_classes` | `[]` | classes always denied |
| `governance.max_output_chars` | `20000` | tool output cap |
| `budget.max_cycles` / `max_model_calls` / `max_subagents` | `200` / `400` / `20` | hard stops (mission is paused) |
| `budget.max_cost_usd` / `max_wall_clock_seconds` | `null` | optional stops |
| `memory.min_importance_to_store` | `0.35` | selective write threshold |
| `memory.default_ttl_days` | `null` | expiry for new memories |
| `memory.consolidation_interval_cycles` | `10` | consolidation cadence |
| `memory.max_retrieval_items` | `12` | defined; not read by the runtime |
| `checkpoint_every_cycles` | `1` | defined; the loop snapshots every 5 cycles and on stop regardless |
| `workspace_max_chars` | `12000` | size of the executive's workspace digest |
| `trace_to_stdout` | `false` | mirror traces to stdout (`-v` overrides) |

## Environment variables

| Variable | Effect |
|---|---|
| `COGOS_HOME` | state directory (overridden by `home` in yaml and by `--home`) |
| `COGOS_CONFIG` | path to the yaml file (default `<repo>/cogos.yaml`) |
| `COGOS_EXECUTIVE_MODEL` | overrides `executive.model` |
| `COGOS_ADAPTER` | overrides `executive.adapter` |
| `ANTHROPIC_API_KEY` | required only by the `anthropic_api` adapter |

Global CLI flags (before the subcommand): `--adapter`, `--model`, `--home`, `--json`, `-v/--verbose`.

## Commands

| Command | What it does |
|---|---|
| `init [--no-config]` | create store and default config; print `health()` |
| `mission new "<objective>" [--context "<text>"] [--run] [--max-cycles N]` | compile a mission (prints id, kind, task/criteria/unknown counts); `--run` starts it |
| `run [mission_id] [--max-cycles N]` | run or resume; without id uses the boot ranking; exit 0 for `complete|active|paused|blocked_external`, 1 if nothing to run, 2 for `failed`/`abandoned` |
| `resume [mission_id]` | alias of `run` |
| `boot [--brief]` | recovery report: store health, git, environment, missions, resume target, unresolved tasks, latest verification, pending events, human requests, workspace |
| `status [mission_id]` | progress, confidence, criteria, task counts, ready tasks, open unknowns, claims/evidence counts, blocked operations, human requests, usage, synthesis, notes, last checkpoint |
| `missions` | list all missions with status and version |
| `trace [mission_id] [--kind K] [--limit N]` | timeline lines (`ts cNNN kind mission summary [cost]`) |
| `explain [mission_id]` | decisions, operations, tool calls, specialists, failures, retries, verifications, total cost, latest assessment |
| `workspace [mission_id]` | the digest the executive sees when selecting the next step |
| `answer <mission_id> <request_id> "<answer>" [--grant CLASS]` | answer a human request, optionally granting an action class |
| `authorize <mission_id> <action_class>` | grant an action class |
| `correct <mission_id> "<text>" [--kind correction|information]` | send a correction or new information |
| `event <kind> [--mission-id ID] [--payload JSON] [--source S]` | emit an external event (untrusted unless source is `human`/`system`) |
| `checkpoint [mission_id]` | export a snapshot; prints the path |
| `import <snapshot> [--overwrite]` | import a snapshot |
| `memory stats|search <query>|consolidate|contradictions [--limit N]` | memory subsystem |
| `skills list|propose <mission_id>|evaluate <mission_id> [candidate_id]` | skill compiler |
| `eval [--suite acceptance|adversarial|all] [--write PATH]` | evaluation harness (scripted adapter) |
| `demo [--objective TEXT] [--max-cycles N]` | offline end-to-end run in a temp workspace |
| `health` | store health JSON |

Without a `mission_id`, `status`, `trace`, `explain`, `workspace` and `checkpoint` use
`kv.last_mission_id`.

## Typical session

```bash
python -m cogos mission new "Build the feature described in REQUIREMENTS.md." --run --max-cycles 40
python -m cogos status
python -m cogos trace --kind blocked
python -m cogos explain --json | jq '.failures, .verifications'
python -m cogos workspace
```

`demo` output, for reference (scripted adapter, 8 cycles):

```
[demo] workspace=/tmp/cogos-demo-... mission=msn_... adapter=scripted model=claude-fable-5-1
...
  ... c005 assess         msn_... must_verify:1 completed task(s) have unverified outputs
  ... c005 verify         msn_... task:task_... passed — 2 artifact(s): 2 passed, 0 failed, 0 inconclusive, 0 skipped
  ... c006 verify         msn_... code:task_... passed — 1/1 commands passed, 0 failed, 0 inconclusive; tests: 3 passed, 0 failed, 0 errors
  ... c008 select         msn_... complete_mission: All success criteria are satisfied and verified
  ... c008 verify         msn_... completion gate: passed — all completion gates satisfied
  ... c008 complete       msn_... mission complete: all completion gates passed
  ... c008 learn          msn_... candidate skill proposed: implementation-inspect-files-verify (not promoted until evaluated)

[demo] SUCCESS: mission status=complete
```

## Observing a run

* `-v` streams every trace line as it is emitted; `.cogos/traces.jsonl` keeps the full JSON.
* `trace --kind select` shows what the executive chose each cycle; `--kind assess` the controller
  directives; `--kind blocked` firewall blocks, quarantines, human requests, waits; `--kind verify`
  every verification and completion-gate result; `--kind residency` model residency violations.
* `explain` aggregates cost (`cost_usd`, tokens, `duration_ms`) from trace `cost` fields.
* `status` shows `blocked_operations` as `<operation>: <reason> (unblock: <condition>)` and
  `human_requests` with ids and options.

## Human answer / authorize flow

1. `status` lists `human_requests` (`id`, `kind`, `question`, `options`) and `blocked_operations`.
2. `answer <mission> <hreq_id> "<text>"` records a trusted `human_input` event; add `--grant
   destructive` (or another class) when the request is an authorization. `authorize <mission>
   <class>` grants directly.
3. Either command reactivates a `blocked_external`/`paused` mission. `run <mission>` continues;
   `_update` applies the answer, resolves matching blocked operations and re-pends their tasks.
4. `correct` records a fact from the principal; use `--kind information` for facts that are not
   corrections.

## Events

`event <kind> --mission-id <id> --payload '{"job_id": "ci-123", "result": {"ok": true}}'` stores an
`Event`, routes it to explicit targets plus matching subscriptions, and reactivates routed
missions. Kinds understood by `_update`: `human_input` (trusted only), `new_evidence` (payload
`evidence: {summary, source, kind, supports, contradicts, scope, freshness, lineage}` becomes
untrusted evidence), `test_completed` and `job_completed` (recorded as notes). Subscriptions are
created when the executive selects `wait_for_external_event` or when the mission blocks on
`human_input`. Poll-based sources in `cogos/events/sources.py` (`FileWatchSource`,
`DeadlineSource`, `ScheduledSource`, `ExternalJobSource`, `TestCompletionSource`) produce events
for a scheduler you supply; the CLI does not run one.

## Snapshots

```bash
python -m cogos checkpoint msn_...            # .cogos/snapshots/msn_....json
python -m cogos import .cogos/snapshots/msn_....json --overwrite
```

Snapshots are also written automatically every 5 cycles and at the end of every run. Copy the
`snapshots/` directory off-machine if you need recovery after database loss; memories and
calibration samples are not included.

## Health

`python -m cogos health` prints `path`, `schema_version`, `integrity` (`PRAGMA quick_check`),
`fts` (FTS5 available) and row counts for `missions`, `mission_events`, `traces`, `decisions`,
`memories`, `events`, `skills`. `boot` warns when integrity is not `ok`.

## Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| `boot` warns `executive adapter unavailable: 'claude' binary not found on PATH` | `claude_code` adapter without the CLI | install Claude Code or set `executive.claude_binary`; state is preserved; no downgrade occurs |
| `status` is `blocked_external` with blocked op `executive cognition` | `ExecutiveUnavailable` during select/perform (binary missing, model unavailable, or a residency violation) | restore access to `executive_model`; then `run <mission>` after any event, or `answer`/`correct` to reactivate |
| `status` is `blocked_external` with other blocked ops | an operation outside the action space, a synthesis/replan naming an external dependency, or replans exhausted | read `what_would_unblock`; `authorize` the class, change `cogos.yaml`, or `correct` with the missing information, then `run` |
| `status` is `blocked_external` awaiting a human request | no independent work remained | `answer <mission> <request_id> ...` then `run` |
| note `paused: cycle budget exhausted ...` (or model-call/subagent/cost/wall-clock) | `ResourceLedger.over_budget` | raise the budget in `cogos.yaml` (new missions) or run with `--max-cycles`; `run` turns `paused` back to `active` |
| note `residency violation (<kind>): requested X, served [Y]` and a `residency` trace | the adapter reported a different model | the call was treated as failed; check the model id, CLI version and account access; nothing was downgraded |
| `health` shows `"fts": false` | SQLite built without FTS5 | retrieval falls back to `LIKE` scans automatically; nothing to fix unless performance matters |
| `StoreConflict: stored version N != in-memory M` | two processes saved the same mission | run one runtime per `COGOS_HOME`; reload the mission before saving |
| `run` prints `no unfinished mission to run` | no `active`/`draft`/`paused` mission and no pending events for blocked ones | `missions` to list; `run <id>` for an explicit target |
| mission `failed` with `completion gate unmet after 3 replans` | unmet criteria that replanning could not address | `explain` for the refusal reasons; fix the environment or `correct` the mission; a new mission is usually cleaner |
| interrupted with Ctrl-C | state was saved at the end of the previous cycle | `resume` |
