# Cognitive OS (cogos) — Implementation Plan

Status: IN PROGRESS. This file is the durable plan for the persistent autonomous
cognitive runtime built around Claude in this repository. Update it as decisions
change; a fresh session should read this first, then `.cogos/` state (if present).

## Decisions (with rationale)

1. **Standalone package `cogos/` at the repo root, not a change to Hermes core.**
   AGENTS.md mandates a narrow core; the runtime is a separate, importable
   package with its own CLI (`python -m cogos`) and tests (`tests/cogos/`). It does
   not touch `run_agent.py`, `cli.py`, or the model tool schema.
2. **Python-owned control loop, model-owned cognition.** The perceive→update→
   select→act→verify→learn cycle is deterministic Python (durable, restartable,
   testable). Every cognition point (mission compilation, step selection,
   observation interpretation, specialist work, synthesis, judgment) is a
   bounded, structured-output call to the executive model through an adapter.
   Rationale: a loop that lives inside one LLM context cannot survive context
   loss; a loop that lives in Python can.
3. **Executive adapters.** `claude_code` (headless `claude -p --json-schema
   --model <EXECUTIVE_MODEL>`; the Agent SDK is itself a wrapper over this CLI
   and adds a dependency, so the CLI is used directly), `anthropic_api`
   (optional, lazy import of the `anthropic` extra), and `scripted`
   (deterministic, for tests/evals). Specialists spawned by the Agent Foundry
   inherit EXECUTIVE_MODEL unless cheaper routing is explicitly authorized.
4. **State store: SQLite with versioned migrations** under `.cogos/` (override
   with `COGOS_HOME`). Event log (append-only) + materialized tables + JSON
   snapshots for checkpoint/export. Conversation history is never the source of
   truth.
5. **Capability firewall is a separate layer from cognition.** Every tool call is
   classified and policy-checked; a denial is recorded as a blocked operation
   and never changes the executive model.
6. **Untrusted content is data.** All retrieved text is wrapped, scanned for
   injection patterns, and tagged with trust level before the model sees it.

## Build order (smallest end-to-end first)

- [x] Environment inspection (Claude Code 2.1.267, Python 3.11, uv venv)
- [ ] `cogos/schemas` — typed pydantic models for all state
- [ ] `cogos/persistence` — SQLite store + migrations + snapshots
- [ ] `cogos/adapters` — executive model protocol, scripted + claude_code
- [ ] `cogos/mission` — mission compiler
- [ ] `cogos/tools`, `cogos/governance` — tool fabric + capability firewall + immune system
- [ ] `cogos/executive` — loop + metacognitive controller
- [ ] `cogos/beliefs`, `cogos/world_model`, `cogos/memory`, `cogos/workspace`
- [ ] `cogos/planner` — goal hierarchy + task DAG + priority
- [ ] `cogos/verification` — verifiers
- [ ] `cogos/agent_foundry` — specialists + independent cognition
- [ ] `cogos/simulation` — counterfactual engine
- [ ] `cogos/observability` — traces, decision journal, calibration, resource ledger
- [ ] `cogos/events` — event bus + wake mapping
- [ ] `cogos/skills` — skill compiler
- [ ] `cogos/evaluation` — eval harness + acceptance scenarios A–J
- [ ] `cogos/cli.py` — init/mission/run/status/boot/resume/trace/eval/demo
- [ ] CLAUDE.md, `.claude/skills`, `.claude/agents`, `.claude/settings.json`
- [ ] Docs: ARCHITECTURE, AUTONOMY, MODEL_POLICY, STATE, MEMORY, EVALS, SECURITY, OPERATIONS
- [ ] Makefile targets: test/lint/typecheck/eval/demo
- [ ] Run evals, fix failures, commit, push, draft PR
