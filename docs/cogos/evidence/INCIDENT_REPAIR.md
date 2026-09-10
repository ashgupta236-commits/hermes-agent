# Incident repair — the evidence pipeline after Live Run #2

Live Run #2 ended `paused` with 1 of 5 criteria satisfied, while the deliverables it wrote were
correct and passed 12/12 tests independently. This is the record of the repair: what actually
broke, what was changed, what was rejected, and what is still weak.

The pipeline under repair:

```
AUTHORIZED ACTION -> OBSERVABLE RESULT -> STRUCTURED EVIDENCE -> ARTIFACT/VERSION IDENTITY
  -> CRITERION-SPECIFIC VERIFICATION -> RECEIPT -> COMPLETION GATE
```

| | |
| --- | --- |
| Starting commit | `957c858` (clean tree) |
| Ending commit | see `git log`; the repair is `a0aacf4` → `9f8e255` and follow-ups |
| Baseline gate at start | 471 tests, 20/20 evals, ruff clean, ty clean, demo completes |
| Gate at end | **528 tests, 20/20 evals, ruff clean, ty clean, demo completes** |
| Live Run #3 | **not executed** — protocol prepared in [`LIVE_RUN_3_PROTOCOL.md`](LIVE_RUN_3_PROTOCOL.md) |

Repository-wide `pytest` also collects `tests/integration/test_ha_integration.py` and siblings,
which error at collection for missing Home Assistant dependencies. That is a pre-existing Hermes
condition, unrelated to cogos and unchanged by this work; the cogos gate is `tests/cogos`.

---

## 1. Confirmed root causes

Each was established by reading the code path and by reproducing the behaviour, not by inference.

### F1 — purpose was conflated with execution mechanism

A task carries two separable things: a **purpose** (`VERIFY`, `FALSIFY`) and an **execution
mechanism** (an authorized tool plan). `_perform` ran tools for exactly seven operation kinds and
neither of those two was among them (`loop.py:673`). `_calls_from_task` (`loop.py:841`) knows how
to turn `parameters {"tool": …, "arguments": …}` into a tool call, but it was reachable only from
that branch. `_verify` recognised only `commands` / `test_command` / `verify_commands`, so a task
whose plan was in the other shape fell through to `engine.verify_task`, which returns
`INCONCLUSIVE "no verifiable output declared"`. That reached `_judge_verification`, whose prompt
interpolates `task.result_summary` — empty, because nothing ran — and the executive failed the
task for producing no evidence.

**Evidence from the live mission.** The three failing tasks each carried a complete plan:

```
task_1m26hpxfw06de9490  {"tool": "shell", "arguments": {"command": "cd /tmp/cogos-live-run-2 …
                          pytest -q -rA 2>&1; echo EXIT_CODE=$?", "cwd": …, "timeout_seconds": 120}}
```

while the two that produced deterministic evidence differed *only* in the parameter key shape:

```
task_1m26h69c9628e9e76  {"cwd": "/tmp/cogos-live-run-2",
                         "commands": ["…/python -m pytest -q"]}   -> receipt ver_1m26hwf9c6a0124ce
```

Cycles 3, 4, 7 and 8 contain **zero `tool_call` traces**, and `usage.tool_calls == 11` is exactly
c1(4) + c2(4) + c9(3). The result was not dropped in transport; the tool was never called. The
executive diagnosed this itself at c8 —

> "Operation routing: task carried execute_code parameters but was run as operation=falsify, which
> yields specialist reasoning rather than tool execution; no deterministic evidence …"

— and worked around it by prefixing later task descriptions with `MUST run as
operation=execute_code via the shell tool, not falsify`. That a frontier model had to invent a
prompt-level workaround for a dispatch bug is the finding.

### F2 — execution was inferred from an exit code, not observed

`verify_code` set `status = PASSED` when `res.ok`, and `res.ok` was `proc.returncode == 0`
(`fabric.py:315`). The pytest counts were parsed two lines earlier and used only to build the
summary string. So **any** exit-0 command was a passing test verification.

Measured on the frozen baseline, 3 of 4 zero-execution probes passed:

| probe | baseline | repaired |
| --- | --- | --- |
| exit 0, zero tests collected (`python -c "print(42)"`) | **passed** | inconclusive |
| program output forgery (a script printing "5 passed") | **passed** | inconclusive |
| all tests skipped | **passed** | inconclusive |
| pytest exit 5, no tests collected | failed | inconclusive |

Two further defects in the same area, neither in the original report:

* **The count parser could be forged by the process under test.** `_PYTEST_COUNTS` was an
  unanchored scan of stdout+stderr, so a one-line script printing `Report: 5 passed, 0 failed`
  produced a PASSED verification with five fabricated executed tests.
* **Test records had no scope.** `verify_criterion` accepted *any* passing record newer than the
  criterion, so an unrelated green suite satisfied a differently scoped criterion.

### F3 — nothing registered a file the executive wrote

`state.artifacts.append(...)` existed at exactly one site, inside the loop over specialist reports
(`loop.py:1505` at the frozen commit). A file written by the executive's own `write_file` never
entered the ledger, so `mission.artifacts` was `[]` for the whole run and the gate's
`required_artifacts` check could not pass.

**Registration alone would not have been enough**, and this was not in the original report:
`required_artifacts` compared declared refs against artifact **ids and names only**, while the live
mission declared absolute **paths** (`/tmp/cogos-live-run-2/calc.py`). A fully registered, verified,
intact artifact at exactly that path would still have failed the check.

---

## 2. Hypothesis revised

The working hypothesis — "execution semantics, test-evidence semantics, and artifact registration
materially blocked Live Run #2" — is **confirmed**, with one correction and one addition.

* **Corrected:** F2 was described as "a zero-collected run is reported as passed". A genuine
  pytest zero-collection run exits 5 and *was* failed. The defect is broader and simpler: the
  status came from the exit code, so anything that exited 0 counted.
* **Added:** the three defects were not sufficient to explain the failure. `required_artifacts`
  matched no paths (F3-b), and a **false completion** existed independently of all of them: a
  mission with correct code, passing tests and nothing registered gated `passed` both before and
  after its implementation was replaced with `return 999.0`.

---

## 3. What changed

| Area | Change | Invariant it establishes |
| --- | --- | --- |
| `loop.py` | `_execute_task_plan` runs a VERIFY/FALSIFY task's declared plan through the ordinary `_tool` funnel; `_verification_from_observations` turns the results into the verification | Tool-backed verification executes and its real result reaches the verifier |
| `loop.py` | verdict comes from `res.ok` / `error_kind` only; output is recorded as detail | A file's contents cannot argue the verifier into a verdict |
| `verification/test_outcome.py` | `classify_test_run` decides from structured evidence; `parse_test_output` is anchored on the runner's summary line; `detect_framework` reads the **command** | Execution is observed, not inferred, and a process cannot report its own test results |
| `engine.py` | `verify_code` records exit code, counts, executed, framework, cwd and criterion scope | "Did the required tests run?" is answerable from state |
| `engine.py` | `verify_criterion` requires criterion binding **and** observed execution | An unrelated or zero-execution run proves nothing |
| `loop.py` | `_register_artifact_candidate` is the single registration site, used by the write path and the specialist path | Candidates are hashed, bounded to writable roots, versioned, and never verified on registration |
| `schemas` | `Artifact.origin/versions/size_bytes/observed_at/mission_id/produced_by_action_id`; `TestRecord.criterion_ids/counts/executed/exit_code/framework`; `VerificationResult.input_versions/produced_by_task_id/produced_by_action_ids`; `VerificationCheck.authoritative` | Machine-readable provenance in both directions |
| `engine.py` | criterion receipts inherit input versions; the gate re-reads them; `required_artifacts` matches ids, names **and** resolved paths | A receipt for version A does not prove version B |
| `loop.py` | `_withdraw_criteria_with_stale_receipts` un-satisfies criteria whose receipts went stale | Changed inputs invalidate current proof and force re-verification, without blocking recovery |
| `loop.py` | judgement may not upgrade a non-passing **authoritative** check | Model reasoning is supplemental to execution evidence, never a substitute |
| `firewall.py` | the `tests` substrate is classified and gated exactly like `shell` | The verification channel is not an unclassified execution primitive |

### Rejected designs

Each of these would have made the fixture complete and weakened the gate. None was implemented.

1. **File existence as criterion proof.** `return 999.0` exists too.
2. **Registration as verification** — letting a candidate imply `verified=True`, or letting the
   artifact branch match unverified artifacts.
3. **Letting the firewall read `ToolCall.purpose`**, or pre-authorising an action class for
   VERIFY/FALSIFY tasks. Purpose is declared by the same model that supplies the command, so this
   would let any operation relabel itself into authority. The firewall still never reads purpose;
   the `tests` bypass was fixed at the substrate.
4. **Supplying `expect_zero` after seeing an empty result.** The exception is only honoured when
   the verification contract declared it before execution.
5. **Back-filling `verified_hash` for legacy artifacts.** No record establishes that the bytes now
   present are the bytes that were checked; this would manufacture verified status precisely where
   provenance is missing. Legacy artifacts stay unverified and are re-verified once.
6. **Treating an empty `input_versions` as "unchanged".** Unknown is not the same as unchanged.
7. **A filesystem crawler** to populate the ledger. Registration is driven by observed authorized
   writes, with before/after hashing, so a pre-existing file cannot be laundered in.

---

## 4. Defects found *by* this repair

Three in the original scope, five more found while repairing, and four found by adversarial review
of the repair itself. All are fixed and regression-tested.

| id | defect | how found |
| --- | --- | --- |
| F2-b | program output could forge test counts | investigation |
| F2-c | test records carried no criterion scope | investigation |
| F3-b | `required_artifacts` matched ids and names, never paths | investigation |
| J1 | executive judgement upgraded a firewall **denial** from INCONCLUSIVE to PASSED | writing the F1 regression |
| G1 | the first `receipt_input_versions` gate check was **vacuous** — it resolved only criterion receipts, which carried no input versions, so its loop body never ran and it always reported PASSED | adversarial review |
| G2 | a mission whose implementation was swapped after verification gated "all completion gates satisfied" | adversarial review |
| G4 | judgement grounding had F2-c's scope defect | adversarial review |
| R1 | `run_tests` was an unclassified execution primitive: the same destructive command was DENIED as `shell` and ALLOWED as `run_tests` | adversarial review |
| R5 | a specialist could launder any existing path into the ledger with no hash, origin or boundary check | adversarial review |
| R6 | two separate post-run reads left a window for an undetected mid-verification change | adversarial review |
| R7 | `InputVersion` identity was a path string, not a resolved path | adversarial review |

G1 is the one worth dwelling on: the check looked correct, the suite was green, and one of the
regression tests was named for exactly the defect it failed to catch — it passed because a
*different* guard (`artifact_integrity`) happened to catch that particular case. A green suite is
not evidence that a check can fail.

---

## 5. Measured before/after

Same offline fixture, same configuration, run against a worktree at `957c858` and against the
repaired tree. Every number is a directly observed runtime result. **This is offline measurement
only** — it says nothing about frontier-model cost, call counts or token usage, which can only be
established by a controlled live run.

| probe | baseline `957c858` | repaired |
| --- | --- | --- |
| Tool-backed VERIFY executes its plan | ✗ | **✓** |
| Tool-backed FALSIFY executes its plan | ✗ | **✓** |
| Authorized actions the verifier observed | 0 | **1** |
| Verification produced from an empty result | ✓ (the defect) | **✗** |
| Zero-test false passes | **3 of 4** | **0 of 4** |
| Unrelated suite satisfies a differently scoped criterion | ✓ (the defect) | **✗** |
| Artifact candidates registered from 2 authorized writes | 0 | **2** |
| Candidates carrying version identity | 0 | **2** |
| Candidates verified on registration | 0 | 0 *(correct in both)* |
| Genuine criterion receipts on a correct implementation | 1 | 1 *(correct in both)* |
| **False completion after a post-verification swap, nothing registered** | **✓ (gate passed)** | **✗ (gate refuses)** |
| False completions in the 3 negative controls | 0 of 3 | 0 of 3 |
| Destructive command via `shell` | denied | denied |
| Destructive command via the test runner | **allowed** | **denied** |

Definitions: a "zero-test false pass" is a `verify_code` call returning PASSED for a command that
executed no test body (denominator 4: exit-0 zero-collection, program-output forgery, all-skipped,
pytest exit 5). A "false completion" is `mission_completion_check` returning PASSED when the
mission has not demonstrated what it claims (denominator 3 for the controls: wrong implementation,
missing implementation, and post-verification swap).

**A behaviour change worth stating plainly.** A mission that runs tests but registers or declares
no inputs can no longer complete: its proof names nothing that can be re-read, so the gate refuses.
That is a deliberate tightening in the conservative direction. Legitimate paths are unaffected —
authorized writes register automatically, and the demo, all 20 eval scenarios and the
failure-recovery acceptance scenario all still reach `complete`.

---

## 6. Persistence and resume

No SQL migration is required or written. A mission is persisted as one opaque `state_json` blob
over a table with no per-field columns, so additive fields need no DDL; compatibility is carried
entirely by pydantic defaults.

Verified on the **real pre-repair records**: both the Live Run #1 and Live Run #2 mission snapshots
load under the current schema, and neither acquires provenance it never earned — a legacy artifact
stays `origin=declared` with no version history, a legacy test record has no criterion scope and no
counts, a legacy receipt has no input versions, and the gate still refuses both missions for the
reasons they were refused live.

The discipline this depends on, now guarded by a test:

* every persisted field has a default — a required field would make every stored mission
  unloadable, and `load_mission` lets the error propagate into boot;
* the default must be the value that **denies** credit;
* "unknown" must stay distinguishable from "zero" wherever a gate reads the number.

Existing precedent kept: a legacy artifact marked `verified=True` with no `verified_hash` is
treated as **unverified** and re-verified once, rather than back-filled.

Resume coverage added: an unverified candidate survives as unverified; a completed verification
keeps its exact input-version identity; an artifact changed after verification is caught on resume;
cumulative spend, budget and task state survive; legacy and real live snapshots load.

---

## 7. Remaining weaknesses

Honest limits of this repair.

1. **Registration is write-driven, so files already on disk are not candidates.** Resuming a
   mission does not re-register deliverables written before the repair or by an unobserved
   mechanism. This is deliberate — the alternative is a filesystem crawler — but it means a resumed
   legacy mission must rewrite or explicitly declare its artifacts.
2. **Covered write mechanisms are `write_file`, `append_file` and shell writes the firewall's own
   extractor identifies.** Writes by a spawned process the extractor cannot see, and writes through
   the `git` tool, are not registered.
3. **Version binding is bounded to the verification contract**: declared `input_paths` plus the
   artifact ledger. It is not a dependency graph. A test whose behaviour depends on an unregistered,
   undeclared file is not bound to that file.
4. **`input_paths` is model-supplied.** A model that declares nothing binds nothing — which the
   gate now refuses rather than silently accepting, but the *choice* of what to bind still sits
   with the party being verified unless artifacts carry it.
5. **Two gate checks still SKIP rather than enforce** when nothing is registered or declared
   (`artifact_integrity`, `required_artifacts`). The receipt-version check now covers the case that
   made this exploitable, but a skip is structurally weaker than a pass.
6. **Injected content can still reach the executive judge's prompt** as check detail and state
   summaries. It cannot change a deterministic status — that is what `authoritative` enforces — but
   the judge is not immune to it.
7. **A contradiction can still be resolved by executive prose** with no observation attached. Out
   of scope here; it does not bear on F1–F3.
8. **TOCTOU is bounded, not eliminated.** Inputs are snapshotted before and read once after, and a
   change in that window is flagged, but nothing prevents a change between the gate's read and a
   later action.

## 8. Documented, not repaired (out of scope)

From Live Run #2, still true and deliberately untouched: H1 temporal settlement was never
exercised; the L1 digest tier has never engaged; all five observed escalation classifications were
`L2_FULL`; no progress observation was emitted; the reality anchor was never reached. The
untrusted-content escalation reason remains broad enough that essentially any tool-bearing
observation escalates to full interpretation — noted, not changed, because narrowing it is a
cognition-cost question rather than an evidence-integrity one.

**The reality anchor is still not live-validated.** It has never run in a live mission, because no
live mission has reached a completion attempt. Nothing in this repair changes that, and it must not
be reported as validated until it actually occurs.
