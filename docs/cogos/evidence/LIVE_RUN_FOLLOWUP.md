# Live-run incident review — root cause and repairs

Follow-up to [`LIVE_RUN.md`](LIVE_RUN.md), which stands unchanged as the record of what happened.
This document is what the incident *taught*, and what was changed because of it.

Baseline for every comparison below: commit `33446ef`, the tree as it stood when the live run
finished.

## 1. Root cause

The live mission solved its task and could not finish it. Correct `calc.py` and `test_calc.py`
were on disk — 4/4 tests pass when run independently — and after 11 cycles and $20.51 not one
verification receipt had been bound, so the completion gate correctly refused.

Reading the mission database rather than the narrative, the spend decomposes cleanly:

| Cognition kind | Calls | Cost | Output tokens | Avg |
| --- | ---: | ---: | ---: | ---: |
| `interpret` | 9 | **$13.69 (70%)** | 185,387 | $1.52 |
| `select` | 9 | $5.06 | 75,564 | $0.56 |
| `specialist` | 3 | $1.07 | 13,282 | $0.36 |
| `challenge` | 2 | $0.46 | 3,756 | $0.23 |
| `replan` | 1 | $0.23 | 2,170 | $0.23 |

All 21 tool calls — `list_dir`, `read_file`, `shell`, `write_file`, `search_text` — were
effectively free. Cycles 5, 7, 8 and 10 alone accounted for $13.55, and every one of them names
the same contradiction in its own decision rationale.

Three mechanisms, compounding:

**A stale observation became a permanent contradiction.** In cycle 1 the mission recorded
`"calc.py and test_calc.py do not exist yet"` as ESTABLISHED at 0.91. It then created those files.
The claim did not become false — it became *historical*. The belief graph had no time axis, so
`detect_contradictions` read the mission's own successful write as a severity-1.0 conflict against
itself. The record is machine-generated: its description is the literal format string from
`graph.py:362`.

**The contradiction pinned the controller.** `contradiction_level` saturated at 1.0, which
re-issued `must_falsify` every cycle and set `effort="max"` on every cognition call. The executive
diagnosed the problem correctly — it wrote *"RESOLVED by time scope: the baseline claim describes
the pre-write state… the existence claim describes the post-write state"* — and had no channel to
act on it, so it appended a second record while the original stayed open.

**Selection never reached the work that would have closed the mission.** This is the part that
matters most: the plan already held **eight** ready or pending verification tasks, one with
parameters `{"cwd": "/tmp/cogos-live-run", "commands": ["…/python -m pytest -q"]}` addressing the
exact criterion the gate was failing on. The machinery was not missing. `must_falsify` outranked
it every cycle, and the planner gave a verification task a +0.15 nudge against a 0.95-priority
exploration.

So: the runtime reached for frontier cognition where a `list_dir` was authoritative, and never
reached for the cheap action that would have finished the job.

## 2. Repairs

Each is the smallest general mechanism for its class of failure. None is specific to Python,
tests, or any filename.

| # | Failure mode | Repair |
| --- | --- | --- |
| A | Mutable facts treated as eternal propositions | Claims carry `observes_current_state`, `subjects`, `observed_at`, `superseded_by`. A later observation of the same subject **supersedes** an earlier one instead of contradicting it. Subject extraction is by path *shape*; only propositions that assert current state are time-scoped, so `"calc.py should use round()"` is correctly left alone. |
| A/C | Deliberating over questions the world answers | `TemporalSettler` settles a contradiction by *looking* — through the real tool fabric — before any falsification is spent. Disputes current state does not overtake are untouched and escalate as before. |
| B | `must_falsify` re-issued forever | Falsification is bounded per target; a belief that survives repeated attack is recorded as resistant rather than attacked again. |
| C | One frontier call per observation, at growing cost | An escalation ladder: L0 handles a clean trusted tool result with no model, L1 reads untrusted content through a reduced schema that cannot revise beliefs, L2 keeps the full contract. Everything that could carry judgment escalates *and says why*. |
| E | Finished work never closed | Closure reads the completion gate's own failed checks, finds already-planned tasks that would bind that evidence, and makes them dominate ordering. |
| F | Budget checked at cycle start, breached mid-call | Admission control decides whether the *next* call fits, using an estimate learned from the mission's own measured calls, and bounds the call provider-side with a per-call `--max-budget-usd`. |
| G | Activity mistaken for progress | Progress is the vector of externally checkable outcomes. Rising spend against a flat vector is surfaced as an observation — never an instruction. |
| I | Trace could not identify its own build | Missions and each run segment record the implementation's commit, dirty state, adapter version, model, config hash and platform. |

Two bugs the reproduction exposed on the way: the mission could state its position, and its
synthesis could conclude, with a claim the world had already superseded; and a `BlindnessViolation`
crashed the run rather than holding. An anchor that cannot be built blindly now fails closed.

### What was deliberately *not* built

An adversarial review of a more aggressive closure design found it would have weakened the gate:
auto-running `verify_criterion` without `evidence_ok`, auto-registering artifacts from a declared
path, and accepting any passing test record newer than the criterion would each have converted
"a file exists" into a PASSED completion predicate. Those paths were not taken. **Closure moves the
queue; only verification moves the evidence.** Nothing added here can satisfy a criterion, register
an artifact, or manufacture a receipt, and tests pin that.

## 3. Before and after, same inputs

The live pattern reproduced as `adv_unclosed_work`: deliverables absent, a stale "these files do
not exist" claim, a severity-1.0 contradiction, and a verification task in the plan. Run against
the baseline worktree and against the repaired tree.

| | Baseline `33446ef` | Repaired | Change |
| --- | ---: | ---: | ---: |
| Status | `paused` (subagent budget exhausted) | **`complete`** | — |
| Completion gate | failed | **passed** | — |
| Cycles | 21 | **10** | −52% |
| Model calls | 60 | **21** | −65% |
| Subagents spawned | 20 | **3** | −85% |
| Criteria with bound receipts | 0 of 2 | **2 of 2** | — |
| Test runs | 0 | **1** | — |
| Deliverables written | none | `calc.py`, `test_calc.py` | — |
| Unresolved serious contradictions | 1 | **0** | — |

The baseline never wrote the deliverables at all: falsification consumed every cycle until the
subagent budget ran out.

**The control that matters.** Same mission, but nothing ever produces the deliverables. Closure
applies all its ordering pressure and the gate still refuses: status `paused`, 0 receipts bound, 0
criteria satisfied, no files. Pinned as `test_no_false_completion_when_the_work_was_never_done`.

## 4. Validation

```
.venv/bin/python -m pytest tests/cogos -q   # 471 passed (was 428)
make cogos-lint                             # ruff clean
make cogos-typecheck                        # ty clean
make cogos-eval                             # 20/20 scenarios (was 19/19)
make cogos-demo                             # verified completion
```

All twelve evaluation metrics remain at target: task completion, correctness, recovery after
failure, evidence quality, hallucination rate, unnecessary human questions, unnecessary agent
spawning, duplicate work, context recovery accuracy, decision consistency, test pass rate, mission
state integrity.

New tests: `test_temporal_truth.py` (11), `test_closure_and_escalation.py` (15),
`test_resource_admission.py` (16), plus the `adv_unclosed_work` adversarial scenario.

## 5. Remaining weaknesses

- **The reality anchor is still not live-validated.** It was not exercised in the live run because
  the mission never reached a completion attempt, and nothing here changes that. Its status stays
  as recorded in `LIVE_RUN.md`.
- **Subject extraction is path-shaped.** Temporal supersession recognises mutable subjects that
  look like file paths. A mutable fact about a database row or a remote resource is not yet
  time-scoped unless the executive marks it so.
- **Admission estimates are per-kind maxima** learned from the mission's own history. A call that
  is atypically larger than anything seen before can still overshoot by one call — bounded now by
  the provider-side per-call ceiling, but not eliminated.
- **The L1 digest tier is unmeasured against a real model.** Offline it is exercised by a
  deterministic handler. Whether a bounded schema degrades interpretation quality on real
  observations is exactly what a live run would test.
- **These are scripted-adapter results.** They demonstrate that the mechanisms work and compose.
  They are not evidence about frontier-model behaviour.

## 6. Is another live run justified?

Yes — and it is now the only way to answer the open questions. The offline reproduction shows the
mechanisms fire and compose; it cannot show what a real model does when they do.

**Proposed mission.** The same objective, workspace and model as the original run
(`claude-fable-5-1`, effort `medium`, the demo project), so the comparison is paired. Budget it at
$12 with a 3600s wall clock — the repaired run should need far less than the original $20.51, and
if it does not, that is the finding.

What it would specifically test, none of which the offline run can:

1. **Temporal settlement under a real belief graph** — does the executive still manufacture the
   ABSENT/PRESENT contradiction, and if so does `TemporalSettler` close it before `must_falsify`?
2. **Closure against real selection** — with the gate's failed predicates in its prompt and the
   ordering boost applied, does the executive actually pick the verification task?
3. **The escalation ladder's quality cost** — how many observations route L0/L1, and does bounded
   interpretation lose anything that mattered?
4. **Admission control against real call sizes** — does the per-call ceiling hold, and does the
   learned cost model converge on the ~$1.52 average interpret cost?
5. **The reality anchor, finally** — a mission that reaches completion runs it, which is the one
   thing the first live run never got to.

Success would be: completion with genuinely bound receipts, materially below $20.51, with the
anchor exercised. Failure would be just as informative, and the four-channel record plus run
provenance now make either outcome attributable to a specific build.

## 7. On self-improvement

This was an engineering cycle: a human-directed incident review in which the system's own live
trace supplied the evidence and a person implemented the repairs. It is **not** autonomous
recursive self-improvement, and nothing here should be read as evidence of it. cogos did not
diagnose or fix itself. The defensible claim is narrower: the live run produced enough structured
evidence — costs per call, decision rationales, contradiction lineage, task state — to locate the
root cause precisely, and the repairs are validated against a reproduction of the original failure.
