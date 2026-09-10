# Model policy

cogos runs a single resident frontier model as its executive. Restrictions on tools, network or
permissions narrow what the runtime may *do*; they never change which model *thinks*.

## Resident executive model

`cogos/config.py`:

```python
DEFAULT_EXECUTIVE_MODEL = "claude-fable-5-1"
"""Resident executive model. Overridden only by explicit configuration."""
```

Resolution order (`load_config`, `cli._runtime`): `--model` flag > `COGOS_EXECUTIVE_MODEL` >
`cogos.yaml` `executive.model` > default. `cogos init` writes `executive.model: claude-fable-5-1`.

Every `CognitionRequest` carries `model=config.executive.model` (or `AgentFoundry.specialist_model()`
for specialists), and every `CognitionResponse` records `model_requested` and `models_used`.

## Separate fields on `MissionState`

| Field | Set by | Meaning |
|---|---|---|
| `executive_model: str` | `MissionCompiler.materialise` from `config.executive.model` | The model this mission was compiled for. Nothing in the loop writes to it afterwards. |
| `capability_state: dict` | `Runtime.new_mission` (per tool: `available`, `reason`); `Executive._tool` and `_verify` add `last_verdict`/`reason` when a call is denied or needs authorization | Which tools exist and how the firewall last treated them |
| `permission_state: dict` | `Runtime.new_mission` (`always_require_human`, `denied_action_classes`, `allow_network`, `allow_shell`, `grants`); `Runtime.authorize` updates `grants` | The governance policy and human grants in force |
| `status: MissionStatus` | the loop and `Runtime` | `draft|active|paused|blocked_external|complete|failed|abandoned` |

A denied tool changes `capability_state`; a grant changes `permission_state`; a blocked external
dependency changes `status`. None of them touch `executive_model`. Scenario
`H_capability_restriction` asserts `state.executive_model == model_before` and that every request
the adapter saw used exactly that model.

## Residency verification

### Adapter level

`ClaudeCodeExecutive._parse` reads `modelUsage` from the CLI JSON result and sets
`residency_ok = self._residency_ok(req.model, list(modelUsage.keys()))`:

```python
def _residency_ok(self, requested: str, used: list[str]) -> bool:
    if not used:
        return True  # nothing recorded (e.g. cached); cannot prove a violation
    req_key = requested.lower()
    for m in used:
        ml = m.lower()
        if ml == req_key or ml.startswith(req_key) or req_key in ml:
            return True
    # Aliases: 'fable' -> 'claude-fable-*'
    alias = req_key.replace("claude-", "").split("-")[0]
    return any(alias and alias in m.lower() for m in used)
```

The constructor accepts `auxiliary_models_ok=("claude-haiku-4-5",)` but `_residency_ok` does not
consult it; an auxiliary model showing up alone in `modelUsage` is a violation.

`AnthropicApiExecutive.call` sets `residency_ok` to whether the returned `msg.model` starts with
`req.model.split("-2")[0]`.

### Executive level

`Executive._cognition` is the single path for the executive's own calls (`select`, `interpret`,
`synthesize`, `replan`, `challenge`):

```python
if req.kind in EXECUTIVE_KINDS:
    self._check_residency(state, resp, req.kind)
    if resp.ok and not resp.residency_ok:
        # Never accept a silently downgraded executive: treat as a failed call.
        return resp.model_copy(update={"ok": False, "error": f"model residency violation: requested {resp.model_requested}, served by {resp.models_used}", "error_kind": "unavailable"})
    if not resp.ok and resp.error_kind == "unavailable":
        raise ExecutiveUnavailable(resp.error)
```

`_check_residency` appends a `residency violation (<kind>)` note to the mission and emits a
`residency` trace whenever `residency_ok` is false. A downgraded response is therefore never
parsed as cognition: it becomes an `unavailable` failure, which raises `ExecutiveUnavailable`.

`Executive.cycle` catches `ExecutiveUnavailable` around `_select` and `_perform` and calls
`_block_on_executive`:

* `status = BLOCKED_EXTERNAL`
* `BlockedOperation(operation="executive cognition", reason="executive model unavailable: ...",
  what_would_unblock="access to executive model '<executive_model>' (no downgrade is performed)")`
* note `blocked_external: executive model unavailable (...); model residency preserved`
* trace `blocked` with `executive_model`, and the run stops. A task that was mid-perform is reset
  to `PENDING`.

`ExecutiveUnavailable` is also raised by `ClaudeCodeExecutive.call` when the `claude` binary is not
on `PATH`, and by `AnthropicApiExecutive` when `ANTHROPIC_API_KEY` is unset or the `anthropic`
package is missing. `Runtime.boot` reports the first case as a warning ("mission state preserved;
no downgrade").

Two boundaries to be precise about:

* Specialist runs (`kind="specialist"`) go through `AgentFoundry.run`, not `_cognition`. The loop
  calls `_check_residency(state, run.response, "specialist")` so a violation is noted and traced,
  but the report is still used.
* `MissionCompiler.compile` calls the adapter directly and falls back to `default_compilation` on
  any failure; the `compile` kind is listed in `EXECUTIVE_KINDS` but residency is not enforced on
  that path.

Deterministic fallbacks (`_fallback_select`, `HeuristicExecutive._interpret`, heuristic replans,
`default_compilation`) are Python, not another model. They keep the loop alive after a transient,
structural or schema error; they are not a downgrade because no model substitution occurs, and
they never fire for `unavailable` errors on executive kinds.

## Specialist model inheritance

```python
def specialist_model(self) -> str:
    ex = self.config.executive
    if ex.allow_cheaper_specialist_models and ex.specialist_model:
        return ex.specialist_model
    return ex.model
```

Both `executive.allow_cheaper_specialist_models` (default `false`) and `executive.specialist_model`
(default `null`) must be set explicitly for a specialist to run on a different model. The
`challenge` call that compares an independent specialist's position with the executive's always
uses `config.executive.model`.

Specialists are defined by `SpecialistSpec` (role, objective, constraints, evidence standard,
termination criterion, `context_keys`, `tools`, `independent`, `max_turns`); `ROLE_GUIDANCE` adds
role-specific instructions for `researcher`, `engineer`, `debugger`, `skeptic`, `source_auditor`
and others. Independent specialists receive a context slice with claims, hypotheses and the
current synthesis withheld (`AgentFoundry.context_slice(..., withhold_conclusions=True)`).

## Claude Code CLI invocation

`ClaudeCodeExecutive.build_command` produces, in order:

| Flag | Value | Purpose |
|---|---|---|
| `-p` | | headless, single prompt |
| `--output-format json` | | machine-readable result with `usage`, `modelUsage`, `structured_output` |
| `--json-schema <json>` | sanitised pydantic schema | structured output contract |
| `--model <id>` | `req.model` | model pinning |
| `--max-turns N` | `max(1, req.max_turns)` (1 for executive kinds, `SpecialistSpec.max_turns` for specialists) | bounded agentic loop |
| `--no-session-persistence` | | no session files left behind |
| `--permission-prompts none` | | never wait on an interactive prompt |
| `--system-prompt <text>` | `PROMPTS[kind]` | operating constitution |
| `--tools a,b --permission-mode acceptEdits` | mapped via `CLAUDE_TOOL_MAP` | tool-using specialists only |
| `--allowedTools <patterns...>` | `Bash(python*)`, `Bash(pytest*)`, `Bash(git status*)`, `Bash(git diff*)`, `Bash(git log*)`, `Bash(ls*)`, `Bash(cat*)`, `Bash(rg*)`, `Bash(grep*)`, `Bash(find*)`, `Bash(uv run*)`, `Bash(npm test*)`, `Bash(make test*)` | only when `Bash` is in the tool list |
| `--tools "" --permission-mode dontAsk` | | reasoning-only calls (all executive kinds, tool-less specialists) |
| `--add-dir <cwd>` | repo root | only when tools are granted |
| `--effort <level>` | request effort or adapter default | see below |
| `extra_cli_args` | from `executive.extra_cli_args` | appended verbatim |

The prompt (plus rendered untrusted blocks) is passed as the final positional argument. Transient
errors (overloaded, rate limit, 5xx, timeout, network) are retried up to `executive.max_retries`
(default 3) with exponential backoff; `is_error` results mentioning an unavailable or invalid model
are classified `unavailable`.

## Effort

`executive.effort` defaults to `high` (`low|medium|high|xhigh|max`). `MetaCognitiveController`
raises the per-cycle effort to `max` when novelty >= 0.6 and confidence < 0.4, contradiction level
>= 0.5, or stakes >= 0.7; `_synthesize` uses `max` when stakes >= 0.5. Specialists use the
configured default.

## Restrictions narrow the action space, never reasoning

`cogos/governance/firewall.py` docstring: "The firewall never touches the executive model choice - a
denial narrows the action space and nothing else." In code:

* A `DENY`/`REQUIRE_HUMAN` verdict produces a `BlockedOperation` and a `capability_state` entry;
  the `blocked` trace records `executive_model` unchanged.
* `Runtime._configure_tool_availability` marks `web_fetch`, `shell` and `run_tests` unavailable
  under policy; the executive still receives the same prompts and schema.
* `CONSTITUTION` principle 9 states the same rule to the model: "A blocked capability narrows the
  action space, never your reasoning."
* There is no code path that selects a different model in response to a policy verdict, a budget
  breach or a tool failure.
