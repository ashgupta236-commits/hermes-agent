# Live Run #2 — controlled validation of the repaired controller

Second run of the cogos executive against the real `claude_code` adapter, executed as a
controlled experiment against the repairs made after Live Run #1
(`LIVE_RUN.md`, `LIVE_RUN_FOLLOWUP.md`). Live Run #1's record is preserved unchanged; where a
claim made there or in the follow-up is superseded, it is corrected explicitly in
[§ Corrections to earlier claims](#corrections-to-earlier-claims).

This was a **human-directed controlled validation**. It is not recursive self-improvement and
is not self-evolution: a human specified the protocol, the hypotheses, the failure controls and
the pass criteria, and a human-directed session performed the repairs beforehand and the
forensics afterwards. cogos did not modify itself.

---

## 1. Frozen implementation

| | |
| --- | --- |
| Implementation commit | `03f059587f5e75e43466bdabeacb3e325a55790b` |
| Branch | `claude/autonomous-cognitive-os-og6r0p` |
| Working tree at run start | **clean** (`git_dirty: false`, `dirty_files: []`) |
| Mission schema version | 1 |
| Adapter | `claude_code` |
| CLI version | `2.1.267 (Claude Code)` |
| Executive model requested | `claude-fable-5-1` |
| Effort | `medium` |
| Config hash | `5e690996965e936b` |
| Python / platform | 3.11.15 / Linux-6.18.44-fc-v24-x86_64-glibc2.39 |
| Run segments | 1 (no resume; `spend_at_start_usd: 0.0`) |

Recorded by `cogos/provenance.py` at mission start, in `resources.provenance` and
`resources.run_segments[0]`. Provenance records the **implementation** repository, not the
workspace, which is why `workspace_commit` is empty (the workspace is not a git repo).

## 2. Pre-run gate

Run against the frozen commit before the experiment began.

| Check | Floor | Result |
| --- | --- | --- |
| `pytest` (full suite) | ≥ 471 | **471 passed** |
| `ruff check` | clean | **clean** |
| `ty check` | clean | **clean** |
| `make cogos-eval` | ≥ 20/20, all metrics at target | **20/20, all at target** |

Scope note: `make cogos-test` runs `tests/cogos`, which is the cogos suite. A repository-wide
`pytest` also collects `tests/integration/test_ha_integration.py` and siblings, which error at
collection on this container for missing Home Assistant dependencies — a pre-existing Hermes
condition unrelated to cogos and unchanged by this work.

No test was weakened, skipped or hard-coded around to obtain this. The gate passed, so the
experiment proceeded.

## 3. Workspace fidelity

Fresh workspace `/tmp/cogos-live-run-2`, built to be a faithful reproduction of the Live Run #1
workspace: `README.md`, `REQUIREMENTS.md`, `pytest.ini` byte-identical to the Live Run #1
baseline; no `calc.py`, no `test_calc.py`, no pre-run pytest invocation, no hint about Live
Run #1's failure modes anywhere in the mission text. Objective identical:
`Build the feature described in REQUIREMENTS.md.`

## 4. Budget

| | |
| --- | --- |
| Prior spend on this mission | $0.00 (fresh mission) |
| Cost budget | **$12.00** |
| Wall-clock budget | **3600 s** |
| Cycle / call / subagent caps | 30 / 80 / 12 |
| Per-call ceiling | dynamic: `affordable_cost()` = `cap − spent − reserved`, passed to the CLI as `--max-budget-usd` |
| Reserved at stop | $0.00 |
| Actual spend | **$10.4749** |
| Remaining at stop | **$1.5251** |
| Wall clock used | **3351.7 s** of 3600 s |

Unlike Live Run #1 there was **no** external `--max-budget-usd 2.5` in `extra_args`; the
per-call ceiling came solely from the repaired admission path, so the run tests the repair
rather than an outer harness.

---

## 5. Outcome

```
status=paused  cycles=10  model_calls=23  retries=0  cost=$10.4749
in=213,018  out=147,408  tool_calls=11  subagents=1  network=0  wall=3351.7s
progress=0.235   criteria satisfied=1/5
stop reason: "paused: next operation would exceed the cost budget:
              $10.47 spent + $1.97 estimated > $12.00"
```

**The mission did not complete.** The completion gate refused on every cycle and named what was
missing; no false completion was produced. One success criterion — `Full test suite passes` —
was satisfied and **bound to a passing verification receipt**
(`ver_1m26hwf9c6a0124ce`, backed by `code:task_1m26h69c9628e9e76 passed — 1/1 commands passed;
tests: 12 passed, 0 failed`). This is the **first bound receipt cogos has ever produced under a
real frontier model**; Live Run #1 produced zero.

The other four criteria were left unsatisfied, correctly (see § Verification integrity).

Completion gate on the final state — **failed**, on three independent checks:

```
failed   success_criteria     4 of 5 criteria have no passing verification record bound to them
passed   tests                2 test record(s), none failing
failed   contradictions       ctr_1m26j7cwd6aa00b46 (severity 0.70) unresolved
passed   blocked_operations   no unresolved blocked operations on unfinished tasks
passed   human_requests       no blocking unanswered human requests
skipped  artifact_integrity   no verified artifacts to re-check
failed   required_artifacts   missing or unverified: /tmp/cogos-live-run-2/calc.py,
                              /tmp/cogos-live-run-2/test_calc.py
```

The gate is right to fail `required_artifacts`: both files exist on disk and are correct, but
**no `Artifact` record was ever registered**, so there is nothing attested for it to check.
`mission.artifacts` is empty for the whole run. The defect is upstream of the gate — nothing in
the runtime registers a file the executive writes itself (finding **F3**) — and it is the single
largest missing link between this run and a completed one.

Raw evidence: `live-run-2.json` (gate verdict, per-call costs and residency, escalation
classifications, controller history, contradictions, verification records).

### Cognition breakdown

All 23 calls were served by `claude-fable-5-1`. 22 carry cost traces; the 23rd is the
mission-compile call ($0.7027, the difference between the $9.7722 traced in-loop and the
$10.4749 total).

| Operation | Calls | Cost | Avg | Share of traced |
| --- | ---: | ---: | ---: | ---: |
| `interpret` | 4 | $4.7475 | $1.1869 | 48.6% |
| `select` | 9 ok + 1 failed | $3.2397 | $0.3600 | 33.2% |
| `replan` | 4 | $1.1627 | $0.2907 | 11.9% |
| `verify` | 3 | $0.3625 | $0.1208 | 3.7% |
| `challenge` (skeptic) | 1 | $0.2599 | $0.2599 | 2.7% |
| **traced total** | **22** | **$9.7722** | | |

Escalation tier: `classify()` ran at all 5 interpretation points and returned **`L2_FULL` every
time** (c1, c2, c8, c9, c10). `L0_DETERMINISTIC` and `L1_DIGEST` were never taken — see H5.

### Epistemic behaviour

| | |
| --- | --- |
| Claims | 19 (0 tagged `observes_current_state`, 0 superseded) |
| Evidence records | 18 |
| Unknowns | 7 (4 resolved, 3 open) |
| Contradictions | 4 raised, 1 resolved |
| Falsification operations | 1 (`falsify` at c8, one skeptic subagent) |
| Claims falsified (controller ledger) | 1 (`clm_1m26h8dnbad0a72e0`) |
| Disagreements | 0 |
| Learned lessons | 19 |
| Assumptions recorded | 4 |
| Tasks | 26 (7 done, 1 active, 8 ready, 10 pending); 18 carry `addresses_criterion_ids` |
| Task replans (bounded) | 2 tasks × 2 replans (cap 3) |
| Retrieval policy | learned bandit crossed its data-support threshold live at c6: `learned estimate +1.000 over 3 observation(s)`, rising to 7 observations by c10 |

### Closure

The closure assessor fired on **all 10 cycles**, every time naming unmet completion predicates
("2 completion predicate(s) unmet", rising to 3 from c9). It correctly identified, on cycle 1,
that no criterion had a passing verification record bound to it — and kept saying so until one
did.

### Reality anchor

**NOT EXERCISED.** `anchor_assessments` is empty. The anchor runs only immediately before a
completion claim; the mission never reached one, so R1 was never invoked. This is not a pass.

---

## 6. Hypotheses

### H1 — Temporal settlement · **NOT EXERCISED**

Zero claims carried `observes_current_state = true`; zero supersessions occurred; the
`TemporalSettler` therefore never had a candidate to settle. The Live Run #1 failure mode (a
stale "the file does not exist" claim outliving the file) **did not recur**, but that is not
evidence the repair works live — the model simply never emitted a current-state claim about a
file it had changed. The mechanism remains covered only by unit tests
(`tests/cogos/test_temporal_truth.py`, 11 tests) and the offline scripted replay.

### H2 — Bounded falsification · **PASS (bound not reached)**

One `falsify` operation across 10 cycles (c8), one skeptic subagent, one entry in the
controller's `falsified` ledger. `MAX_FALSIFICATION_ATTEMPTS = 2` was never hit because no
target was retried. The `must_falsify` directive appeared on 2 of 10 cycles (c9, c10) against
**7 of 11** in Live Run #1, and — critically — on both of those cycles the executive did *not*
select `falsify`: it selected `execute_code` (c9) and, after the credit failure, fell through to
`direct_reasoning` (c10). The repeated-falsification loop that consumed Live Run #1 did not
recur.

### H3 — Criterion-aware closure scheduling · **PASS**

18 of 26 tasks carried `addresses_criterion_ids`. Verification-shaped work was **selected**,
which is exactly what Live Run #1 failed to do (there, eight ready/pending verification tasks
were never picked). `verify` was the selected operation on 5 of 10 cycles (c3–c7), and the two
that had machine-checkable commands attached produced deterministic evidence:

* c5 → `task_1m26h69c9628e9e76` (priority 1.0, addresses `sc_…93c46c4d5`) → pytest run, 12 passed
  → the receipt.
* c6 → `task_1m26h69cae99fda58` (addresses two criteria) → signature + value spot-check, passed.

Closure changed the ordering; it did not touch the evidence.

### H4 — Verification integrity · **PASS (strongest single result)**

Closure boosted criterion-verification work to the front of the queue, and the verification
engine still refused to mark criteria satisfied without deterministic evidence:

```
ver_…c10cfe5e0  criterion sc_…9fa1cad56   inconclusive
  method "inspect_files on …/calc.py; import and check signature via inspect.signature"
  is not machine-checkable and no evidence_ok was supplied

ver_…ce72c5d55  criterion sc_…9d8a220cc   inconclusive
  method "execute_code: run the examples and independent spot checks via python -c"
  is not machine-checkable and no evidence_ok was supplied
```

Both criteria were left **unsatisfied**. The deliverables were, in fact, correct — and the
architecture still refused to say so without a machine-checkable check. Closure moved the queue;
only verification moved the evidence. Three further verifications *failed* tasks on executive
judgement for producing no observable output, rather than passing them on plausibility.

### H5 — Cognitive escalation · **PARTIAL / did not deliver the saving**

`classify()` ran 5 times and returned `L2_FULL` 5 times. Reasons recorded:

| Cycle | Operation | Reasons |
| --- | --- | --- |
| c1 | `inspect_files` | untrusted content; task chosen to resolve an open unknown |
| c2 | `execute_code` | 1 tool call failed (`calculate`); untrusted content |
| c8 | `falsify` | operation exists to produce judgment |
| c9 | `execute_code` | untrusted content; 1 unresolved contradiction above severity 0.5; open unknown |
| c10 | `direct_reasoning` | operation exists to produce judgment |

The cheap tiers are effectively unreachable in a tool-using mission: any observation containing
tool output trips "untrusted content must be assessed, never absorbed", and every judgment
operation is excluded by construction. **The 49% cost reduction did not come from the escalation
ladder.** It came from having five fewer full-interpretation cycles (4 vs 9) and from those
cycles being cheaper on average ($1.19 vs $1.52), which in turn came from the controller
spending c3–c7 on the cheap `verify`/`replan` path instead of on interpretation.

Lower cost alone is not success, and it is not claimed as such: the mission still did not
complete.

Worse, the contradiction-driven effort escalation **recurred**. `contradiction_level` reached
**1.0** at c9 and c10 (from a severity-0.7 contradiction the executive itself raised at c8), and
`controller.py:130` forces `effort = "max"` at `contradiction_level ≥ 0.5`. Cycle 9 alone cost
**$2.83** — a $0.86 `select` (14,412 output tokens) plus a $1.97 `interpret` (34,840 output
tokens) — 27% of the entire run, and it is the call whose estimate then tripped admission
control. This is the same failure class as Live Run #1, bounded to 2 cycles instead of 7 but not
eliminated.

### H6 — Progress-gradient control · **INSTRUMENTED, escalation NOT EXERCISED**

The verifiable-progress vector tracked and moved:

```
criteria_with_receipts 1 · verified_artifacts 0 · passing_tests 2 · resolved_blockers 0
resolved_unknowns 4 · settled_contradictions 1 · completed_tasks 7
cycles_flat = 0   spend_since_last_progress = $0.00
```

`cycles_flat` never reached `FLAT_PROGRESS_CYCLES = 3`, so the spend-vs-progress meta-observation
never fired and was never shown to the executive. A separate counter — the controller's own
`stalled_cycles`, which tracks task completion rather than verifiable progress — reached 2 twice
(c4–c5 and c8–c9) and reset before a third. The mechanism is behaving correctly here: it
declined to nag while progress was real, and the progress vector confirms progress *was* real
(1 receipt, 2 passing tests, 4 unknowns resolved, 1 contradiction settled, 7 tasks done). But
its escalation path is untested live.

### H7 — Resource admission control · **PASS, with an explicit limit on the claim**

This is the clearest behavioural difference from Live Run #1. Admission refused the *next* call
**before starting it**:

```
blocked  cognition:interpret refused admission — next operation would exceed the cost budget:
         $10.47 spent + $1.97 estimated > $12.00
         estimate: {cost_usd: 1.97010525, seconds: 381.483}
```

The $1.97 estimate is empirically grounded: it is exactly the cost and duration of the c9
`interpret` call, recorded by `_record_call_cost` and reused by `_estimate_call`. Final spend
**$10.4749 against a $12.00 cap — under budget, with $1.5251 unspent.** Live Run #1 overshot:
$20.51 against $19.00, and 2940 s against 2400 s.

**The limit on this claim, stated per protocol:** $12.00 is not proven to be a mathematically
hard ceiling by this run. Two conditions would have to hold, and only the first is demonstrated:

1. `admit()` refuses before the call — **demonstrated live**.
2. The per-call `--max-budget-usd` ceiling (`cap − spent − reserved`) is enforced provider-side,
   so a call that *is* admitted cannot overrun its own estimate past the cap — **passed to the
   CLI on every call but never binding**, because the largest single call ($1.97) came in well
   under its ceiling at the time ($3.50). We have no live evidence that the CLI enforces the
   flag. Until we do, the honest statement is: admission control prevented the overshoot that
   Live Run #1 suffered, and the design bounds the residual exposure to one call's overrun.

### H8 — Reality anchor · **NOT EXERCISED**

Zero anchor assessments. The mission never approached a completion claim. Not a pass.

---

## 7. False-completion control

No completion was claimed, so the control has nothing to overturn. For completeness, the
deliverables were verified independently after the run (**diagnostic only — this does not and
cannot convert a paused mission into a successful one**):

```
$ cd /tmp/cogos-live-run-2 && python -m pytest -q
............                                                    [100%]
12 passed in 0.01s     exit=0

sig: (value: float, percent: float) -> float
add_percent(100,10)=110.0 · (100,0)=100.0 · (200,-25)=150.0
add_percent(10,3.333)=10.33 · (19.99,7.5)=21.49 · (-100,10)=-110.0     all OK
```

The code cogos wrote is correct. **The architecture did not prove it was correct**, for four of
the five criteria. Per §16 of the protocol, the correctness of the generated code is not
evidence for the architecture.

## 8. Failure controls

| Control | Result |
| --- | --- |
| False completion | **Not triggered** — gate refused on every cycle; `status = paused` |
| Missing work accepted as done | **Not triggered** — 3 tasks were failed by verification for producing no observable output |
| Fabricated receipt | **Not triggered** — 1 receipt, traceable to a real pytest run (12 passed) reproducible independently |
| Criterion satisfied without evidence | **Not triggered** — 2 criteria explicitly marked `inconclusive` for non-machine-checkable methods |
| Silent model downgrade | **Not triggered** — see below |
| Corrupted state | **Not triggered** — 11 checkpoints, snapshot loads cleanly, schema v1 |
| Cumulative spend reset | **N/A** — single segment, no resume |
| Resource denial reported as completion | **Not triggered** — denial recorded as `blocked` + `paused` |
| Anchor bypass | **Not triggered** — anchor never reached, recorded as such |
| Uncontrolled loop | **Not triggered** — 10 cycles of 30, bounded replans (2 × 2 of 3) |
| Architecture modified during experiment | **Not triggered** — tree clean at start and at end, HEAD unchanged at `03f0595` |

### Model residency and the credit-exhaustion event

At cycle 10 the provider refused a call outright:

```
error  c10: select failed: You're out of usage credits. Switch to another model to continue.
       error_kind=structural  models_used=[]  residency_ok=true  cost=$0.00
```

**cogos did not switch models.** It recorded the failure, fell through to *deterministic*
selection (not a weaker model), classified the resulting operation, and then hit the budget
refusal. Across all 22 traced cognition calls: `residency_ok = true`, `models_used =
{claude-fable-5-1}`, zero residency-violation traces, zero retries. Constitution §3 held under a
live provider refusal that explicitly invited a downgrade.

**Honest confound, stated per §13:** the recorded stop reason is the budget refusal, and its
arithmetic is independently sound ($10.47 + $1.97 > $12.00, so the run would have stopped there
regardless). But the provider had *already* refused one call in that same cycle for credit
exhaustion. We therefore cannot claim the run would have continued past cycle 10 had the cap
been higher. The two terminating conditions are confounded at the terminal cycle. What is not
confounded: the spend stayed under the cap, and the refusal was issued by cogos' own admission
path before the call, not after it.

---

## 9. Comparison — Live Run #2 vs Live Run #1 (both real frontier runs)

This is the primary comparison. Both runs used the real `claude_code` adapter,
`claude-fable-5-1`, effort `medium`, the same objective and an identical workspace.

| Metric | Live Run #1 | Live Run #2 | Δ |
| --- | ---: | ---: | ---: |
| Cost | $20.5088 | **$10.4749** | **−48.9%** |
| Cost budget | $19.00 | $12.00 | overshoot $1.51 → **underspend $1.53** |
| Model calls | 24 | 23 | −4.2% |
| Input tokens | 265,962 | 213,018 | −19.9% |
| Output tokens | 280,159 | 147,408 | **−47.4%** |
| Cycles | 11 | 10 | −9.1% |
| Cost / cycle | $1.864 | $1.047 | −43.8% |
| Cost / call | $0.8545 | $0.4554 | −46.7% |
| Wall clock | 7297 s | 3352 s | −54.1% |
| Tool calls | 21 | 11 | −47.6% |
| Subagents | 3 | 1 | −66.7% |
| Full `interpret` calls | 9 ($13.69, avg $1.52) | 4 ($4.75, avg $1.19) | **−55.6% calls, −65.3% cost** |
| Cycles carrying `must_falsify` | 7 of 11 (63.6%) | 2 of 10 (20%) | −43.6 pp |
| Verification records | 0 | 8 | +8 |
| **Bound receipts** | **0** | **1** | **+1** |
| Criteria satisfied | 0 / 5 | 1 / 5 | +1 |
| Final status | `paused` | `paused` | **unchanged** |
| Completion gate | refused | refused | unchanged |
| Reality anchor | not reached | not reached | unchanged |
| Segments | 2 (resume) | 1 | — |

### Separately: the OFFLINE scripted incident scenario — **do not merge with the above**

The follow-up (`LIVE_RUN_FOLLOWUP.md`) reported a before/after on the *scripted* adapter, in a
git worktree, on identical inputs: 21 → 10 cycles, 60 → 21 model calls, 20 → 3 subagents,
0 → 2 receipts, `paused` → `complete`. That comparison is **OFFLINE evidence only** and is
reported here only so the two are not confused.

**The two baselines are not the same trajectory.** Real Live Run #1 wrote correct deliverables
(`calc.py`, `test_calc.py`, 4/4 tests passing independently) and then failed closure. The
offline scripted baseline never wrote the deliverables at all. They demonstrate the same
*controller* failure class — metacognitive waste starving verification until the budget ran out
— but they are different trajectories, and the offline `paused → complete` transition has **no
live counterpart**: live, the status is `paused` in both runs.

---

## 10. Experimental honesty

### Resolving the Live Run #1 attribution discrepancy

Two figures were quoted for "how much the contradiction cost": **~$9.28 / 48%** and **~$13.55**.
Recomputed exactly from the Live Run #1 mission database
(`/tmp/cogos-live-run/.cogos/cogos.db`), both are correct measurements of different quantities
against different denominators:

| Slice | Cost | % of $20.5088 mission | % of $19.435 cognition-only |
| --- | ---: | ---: | ---: |
| `interpret` calls in cycles 5/7/8/10 | **$9.2908** | 45.3% | **47.8%** |
| all calls in cycles 5/7/8/10 | **$13.5546** | 66.1% | 69.7% |
| `interpret` calls in all `must_falsify` cycles | $10.7659 | 52.5% | 55.4% |
| all calls in all `must_falsify` cycles | **$15.7506** | **76.8%** | 81.0% |

So: **$9.28** counted only the `interpret` calls in the four cycles that named the contradiction,
and **48%** was that figure as a share of a $19.435 cognition-only subtotal that *excluded* the
three specialist calls — not as a share of the $20.51 mission total (which is 45.3%). **$13.55**
counted *every* call in those same four cycles. Neither number was wrong; they were never the
same measurement, and quoting them side by side without their denominators was the error.

The broadest defensible statement, used from here on: **$15.75 — 76.8% of Live Run #1's total
spend — was incurred on cycles carrying the `must_falsify` directive.** The narrowest
defensible statement is $9.29 (45.3%) for the full-interpretation calls in the four cycles that
explicitly named the contradiction. Neither figure is selected for making the repair look
better; the broader one makes the *problem* look bigger and the repair's remaining gap (H5,
contradiction saturation recurred) look worse.

### What this run does not show

* It does **not** show the temporal settler working live (H1 never fired).
* It does **not** show the escalation ladder saving money (all 5 classifications went to
  `L2_FULL`; the saving came from elsewhere).
* It does **not** show the progress-gradient escalation working live (never triggered).
* It does **not** show the reality anchor working live (never reached).
* It does **not** show a mathematically hard $12 ceiling (the provider-side per-call flag was
  never binding).
* It does **not** show the architecture completing a mission. It shows it refusing to claim one.

---

## 11. New findings from Live Run #2

Three defects this run exposed or pinned down that Live Run #1 did not. **None was fixed during
the experiment** — the tree stayed at `03f0595` throughout, per protocol §9.

### F1 — Operation routing can select a non-tool-executing operation for tool-shaped work

`OperationKind.VERIFY` calls `_verify()` and `OperationKind.FALSIFY` spawns specialists; neither
runs tools (`cogos/executive/loop.py:770` and `:729`). When the executive selects one of these for
a task whose *work* is a shell command, the task produces no result and is then failed for
producing no evidence:

```
c3  verify  task_…w06de9490 failed — "RESULT SUMMARY is empty — no artifact to evaluate."
c4  verify  task_…c77b43f130 failed — "Result summary is empty: no COGOS_PROBE_OK line present"
c7  verify  task_…wd22f225e failed — "RESULT SUMMARY is empty: no cat -n output …"
c8  falsify task_…          failed — "Operation routing: task carried MUST run as
                                      operation=execute_code … but was routed as falsify"
```

Cost: cycles 3, 4, 7 (select + verify + replan each) ≈ **$1.92**, plus cycle 8's mis-routed
falsify chain ≈ **$1.53** — together **≈ $3.44, 33% of the run**. The executive eventually
diagnosed this itself and began prefixing task descriptions with
`MUST run as operation=execute_code via the shell tool, not falsify`, which worked at c9. That
a frontier model had to invent a prompt-level workaround for a dispatch bug is the finding.

### F2 — The verification engine reports a 0-collected run as `passed`

`ver_1m26hx9rc493b844d` records `1/1 commands passed … tests: 0 passed, 0 failed, 0 errors` for a
`python -c` command — a command that collects no tests at all — and the task-level verification
recorded it as `passed`. (The criterion-level verifications for the same task were correctly
`inconclusive`, so no criterion was satisfied on this basis; the defect is the `passed` status on
a zero-collection run and the `tests: 0 passed` evidence it emitted.) The executive noticed and
raised a severity-0.7 contradiction against its own verifier
("… combined with a verification gate that treats 0-collected as PASS"). That contradiction then
saturated `contradiction_level` to 1.0, forced `effort = "max"` for cycles 9–10, and produced the
$2.83 cycle that ended the run. A verification-engine weakness became the run's dominant cost
driver — the Live Run #1 failure class, entered through a different door.

### F3 — Files the executive writes itself are never registered as artifacts

`state.artifacts.append(...)` appears exactly once in the runtime
(`cogos/executive/loop.py:1505`), inside the loop over **specialist reports**. A file written by
the executive's own `write_file` tool call is never registered. Consequently:

* `mission.artifacts` was empty for the entire run, despite `calc.py` and `test_calc.py` existing
  on disk and being correct;
* the gate's `required_artifacts` check failed on both files, and `artifact_integrity` was
  skipped for want of anything to re-check;
* `VerificationEngine`'s artifact-overlap route to satisfying a criterion
  (`engine.py:471`) was unreachable, which is a large part of why four criteria could only have
  been satisfied through machine-checkable verification methods they did not have.

This is structural, affects Live Run #1 identically (also 0 artifacts), and is the single
largest missing link between a run like this and a completed one.

---

## 12. Classification

**B — PARTIAL LIVE VALIDATION.**

Not A. The protocol is explicit: *"Do not choose A because the generated code happens to be
correct. The architecture itself must complete and prove its work."* The architecture did not
complete. `status = paused`, 1 of 5 criteria bound, the anchor never ran, and three of the eight
hypotheses (H1, H6-escalation, H8) were never exercised at all. A run that stops early cannot be
a strong validation of a runtime whose thesis is that it finishes and proves what it finished.

Not C. The run is not a failure. Measured against the actual frontier baseline it halved the
cost, cut output tokens by 47%, produced the first bound verification receipt cogos has ever
obtained from a real frontier model, stopped **under** budget by its own admission control after
Live Run #1 overshot, held model residency through a provider refusal that explicitly invited a
downgrade, and — the result that matters most — refused to mark two criteria satisfied even
though the underlying code was demonstrably correct and closure had pushed that work to the
front of the queue. Every integrity control held.

What stands between B and A is now specific and testable:

* **F3** — nothing registers artifacts for executive-written files, so `required_artifacts` can
  essentially never pass and the artifact route to criterion satisfaction is unreachable. This is
  the blocker, not a cost problem.
* **F1** — operation routing can send tool-shaped work to a non-tool-executing operation
  (≈ $3.44, 33% of the run).
* **F2** — a 0-collected run reported as `passed`, which produced the severity-0.7 contradiction
  that saturated `contradiction_level` to 1.0 and forced `effort = "max"` for the last two cycles
  (≈ $2.83, 27% of the run) via `controller.py:130`.

F1 and F2 together account for roughly half the run's spend; F3 accounts for its inability to
finish. H1, H6's escalation path and H8 remain untested live and must not be reported as
validated.

---

## Corrections to earlier claims

Prior statements are preserved in `LIVE_RUN.md` and `LIVE_RUN_FOLLOWUP.md`; the following are
superseded, not deleted.

1. **"~$9.28 / 48% of Live Run #1 was spent on the contradiction"** and **"~$13.55 was spent on
   the contradiction"** — both were partial measurements quoted without denominators. Superseded
   by the table in §10: $9.2908 is the `interpret`-only slice of four cycles (45.3% of mission
   spend; the "48%" was against a cognition-only subtotal), $13.5546 is all calls in those
   cycles, and the full `must_falsify` exposure is $15.7506 (76.8%).
2. **The follow-up's before/after (21→10 cycles, 60→21 calls, 0→2 receipts, paused→complete)**
   remains accurate *as an offline scripted result* and is now explicitly labelled OFFLINE. It
   must not be read as a prediction of live behaviour: live, the repaired runtime went 11→10
   cycles, 24→23 calls, 0→1 receipt, and `paused`→`paused`.
3. **The follow-up's framing of the escalation ladder (area C) as a cost-reduction mechanism** is
   not supported live. All five live classifications returned `L2_FULL`. The observed saving came
   from fewer full-interpretation cycles, not from cheaper tiers.
4. **The follow-up implied the temporal settler addressed the live failure.** It may; this run
   provides no live evidence either way, because no current-state claim was ever emitted.
