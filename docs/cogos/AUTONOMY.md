# Autonomy contract

The runtime is built to act, not to ask. This document states exactly what it decides on its own,
the few cases in which it stops for a human, and how those rules are enforced in code (not only in
prompts).

## What the system decides itself

* **Interpretation of intent.** `MissionCompiler` turns an objective into success criteria,
  explicit vs inferred constraints, unknowns, hypotheses, goals, a task DAG and risks. The
  `compile` prompt instructs the model to record an assumption and proceed rather than ask when an
  ambiguity can be resolved with a professional default; `Assumption.load_bearing=True` is recorded
  for every compiled assumption.
* **Architecture and implementation choices** inside the writable roots (repository root plus
  `COGOS_HOME`), including creating and editing files, running shell commands, tests and git
  read-only commands.
* **Which step comes next** (`StepDecision`), which specialists to spawn (`AgentFoundry.should_spawn`),
  when to verify, falsify, challenge, synthesize or attempt completion.
* **Retries, replanning and abandonment** of tasks (`Planner.retry_decision`, `_replan_task`).
* **Network reads** (`web_fetch` GET) when `governance.allow_network` is true and the host is not
  denied.
* **Memory, skills, world model and belief updates.**

The `CONSTITUTION` (`cogos/prompts.py`) principle 1 tells the model to "ask the human only for
genuinely non-inferable external decisions (credentials, legal authorization, irreversible
high-impact actions, material financial commitments, mutually exclusive preferences)". The
evaluation scenario `I_autonomy` asserts that "Complete this project." produces zero
`request_human_authorization` selections and zero `HumanRequest`s.

## What requires a human

### Firewall: `always_require_human`

`GovernanceConfig.always_require_human` defaults to
`["destructive", "financial", "legally_significant", "credential_sensitive"]`. In
`CapabilityFirewall._decide`, a tool call whose classified `ActionClass` is in that list and not in
`human_grants` gets `PolicyDecision.REQUIRE_HUMAN`. Classes in `denied_action_classes` get `DENY`
and never produce a human request. `consequential_shared`, `security_sensitive` and
`privacy_sensitive` are allowed by default unless listed in `always_require_human`.

### Where a `HumanRequest` is created

| Site | `kind` | `independent_work_remaining` |
|---|---|---|
| `MissionCompiler.materialise` from `MissionCompilation.human_requests` | as compiled (`authorization|credential|decision|information`) | `True` |
| `Executive._tool` when the verdict is `REQUIRE_HUMAN` | `authorization`, question `Authorize <class> operation: <op>`, options `["authorize", "deny"]` | `True` |
| `Executive._perform` for `OperationKind.REQUEST_HUMAN_AUTHORIZATION` | from `decision.human_request` (default `decision`) | `bool(planner.compute_ready())` |

`Executive._update` recomputes `independent_work_remaining = has_ready` for every unanswered
request on every cycle, so the flag tracks the plan, not the moment of creation.

### `independent_work_remaining` and blocking

A human request alone never stops the mission. The mission blocks only when nothing else can be
done:

* `MetaCognitiveController.assess` emits `escalate:no independent work remains; a human decision
  is required` when there are unanswered requests and no pending/ready tasks.
* `HeuristicExecutive._select` (and the `select` prompt) chooses `request_human_authorization`
  only when `independent_work_remaining` is false.
* `mission_completion_check` fails the `human_requests` gate only for unanswered requests with
  `independent_work_remaining == False`.

### `BLOCKED_EXTERNAL` semantics

`MissionStatus.BLOCKED_EXTERNAL` means "cannot make progress without something outside the
runtime's action space". It is set in `Executive._attribute`, `_replan`, `_replan_task` and
`_block_on_executive`:

| Trigger | Where | Side effects |
|---|---|---|
| `request_human_authorization` with no independent work | `_attribute` | note, subscribe to `human_input` |
| `wait_for_external_event` while unresolved `BlockedOperation`s exist | `_attribute` | note listing `what_would_unblock` (without blocked ops the mission is `PAUSED` instead) |
| `Synthesis.mission_status == "blocked_external"` with the plan exhausted | `_attribute` | `BlockedOperation("mission completion")`, subscribe |
| `Replan.blocked_by` after a completion refusal | `_replan` | `BlockedOperation`, subscribe |
| three replans exhausted with unresolved blocks or unanswered requests | `_replan` | otherwise the mission becomes `FAILED` |
| structural task failure the executive says is externally blocked | `_replan_task` | task `BLOCKED` + `BlockedOperation(task_id=...)` |
| executive model unavailable | `_block_on_executive` | `BlockedOperation("executive cognition")`, no downgrade (MODEL_POLICY.md) |

Every `BlockedOperation` carries `what_would_unblock`; `cogos status` prints them.

Reactivation: `Runtime.answer`, `authorize`, `correct`, `inform` and `emit_event` set a
`BLOCKED_EXTERNAL`/`PAUSED` mission back to `ACTIVE` and save it. `Executive.run` itself turns
`PAUSED` into `ACTIVE` unconditionally but turns `BLOCKED_EXTERNAL` into `ACTIVE` only when
`EventBus.pending_for(mission_id)` is non-empty. `Runtime.resume_target` never picks a
`BLOCKED_EXTERNAL` mission that has no pending events.

### CLI for the human side

```bash
python -m cogos status <mission_id>                       # lists human_requests with ids and options
python -m cogos answer <mission_id> <request_id> "yes, use the staging account" [--grant destructive]
python -m cogos authorize <mission_id> destructive        # grant an action class for this mission
python -m cogos correct <mission_id> "The deadline is Q3, not Q2"           # kind=correction
python -m cogos correct <mission_id> "Budget is 50k" --kind information     # kind=information
python -m cogos run <mission_id>                          # continue
```

* `answer` emits a trusted `human_input` event with `request_id`, `answer` and optional `grant`;
  `_update` marks the request answered, appends the grant to `permissions.grants`, and
  `_apply_grants` pushes it into `CapabilityFirewall.human_grants`, resolves matching
  `BlockedOperation`s and re-pends their tasks.
* `authorize` writes the grant directly into `permissions.grants` and `permission_state.grants`,
  resolves blocked operations of that class, and answers any unanswered request whose question
  mentions the class.
* `correct` adds a `Fact` with `HUMAN_PRINCIPAL` provenance (and a note for corrections).

Only events whose `source` is `human` or `system` can be trusted; `EventBus.emit` forces
`trusted=False` for every other source, so a `human_input` event forged by an external source can
never answer a request.

## Retry and replan policy

`Planner.retry_decision(task, error, error_kind)` (deterministic):

| Condition | Action |
|---|---|
| `error_kind in ("denied", "requires_human")` | `escalate` - task becomes `BLOCKED`; other work continues |
| `error_kind == "transient"` and `attempts < max_attempts` (3) | `retry` with backoff `min(2**attempts, 30)` s (the executive sleeps at most 5 s) |
| same `failure_signature` as the last failure | `replan` - "structurally identical failure repeated" |
| `attempts >= max_attempts` | `abandon` - dependents cancelled (`synthesize` tasks are kept and their dependency dropped) |
| otherwise | `replan` - strategy change before retry |

`failure_signature = sha1(operation_hint | error_kind | normalised error)[:16]` is stored on the
task so an identical retry is never issued twice.

`_replan_task` asks the executive for a `Replan` whose `new_tasks` become prerequisites of the
failed task, which is then re-pended with `_strategy_note`; `blocked_by` blocks the task;
`give_up` exhausts it; an empty plan lowers priority by 0.15 and retries. Heuristic replans exist
for failing tests (`Fix failing tests (attempt n)` up to two attempts), research verification
(`Strengthen evidence`), and "no reasoning model" errors.

Mission-level replanning (`_replan`) runs when the completion gate refuses and the plan is
exhausted; after three replans the mission becomes `BLOCKED_EXTERNAL` (if blocks/requests remain)
or `FAILED`.

Budget breaches (`ResourceLedger.over_budget`: cycles, model calls, subagents, optional cost and
wall clock) set `PAUSED` with a `paused: <reason>` note and stop the run; `cogos run` resumes it.

## Completion definition

A mission is complete only when the executive selects `complete_mission` **and**
`verification.engine.mission_completion_check` returns `PASSED`. The gate checks:

| Check | Passes when |
|---|---|
| `success_criteria` | every criterion has `satisfied == True` and non-empty `verification_ids`; a mission with no criteria is `INCONCLUSIVE` |
| `tests` | the latest `TestRecord` per command name is not `FAILED` |
| `contradictions` | no unresolved `Contradiction` with `severity >= 0.5` |
| `blocked_operations` | no unresolved `BlockedOperation` attached to an unfinished task |
| `human_requests` | no unanswered request with `independent_work_remaining == False` |
| `required_artifacts` | every `resources.required_artifacts` entry matches a verified artifact (skipped when none declared) |

Additional enforcement outside the gate:

* `_select` rewrites `complete_mission` into `verify` while `_verification_pending` is non-empty.
* `_synthesize` clears `criteria_assessment[].satisfied` for criteria without verification records.
* `_apply_interpretation` honours `criteria_satisfied` only for criteria with `verification_ids`.
* `criterion.satisfied` is set exclusively by `VerificationEngine.verify_criterion`.

On a passed gate `_attribute` sets `COMPLETE`, `timestamps.completed_at`, traces `complete`, and
`Runtime.run` proposes a candidate skill. Scenario `J_completion_integrity` asserts that an
executive which keeps selecting `complete_mission` with buggy code is refused every time.
