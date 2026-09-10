# Live validation run — real `claude_code` adapter

First run of the cogos executive against a real frontier model rather than the scripted
adapter. Raw evidence: `live-run-segment1.json`, `live-run-segment2-resume.json`.

| | |
| --- | --- |
| Date | 2026-09-10 |
| Adapter | `claude_code` (Claude Code CLI **2.1.267**) |
| Executive model | `claude-fable-5-1`, effort `medium` |
| Workspace | `/tmp/cogos-live-run` (the standard demo project) |
| Objective | "Build the feature described in REQUIREMENTS.md." |
| Cycles | 11 across two segments (1–6, then a resume 7–11) |
| Model calls | 24 (0 retries) |
| Tokens | 265,962 in / 280,159 out |
| Cost | **$20.51** |
| Wall clock | ~2 hours |
| Final status | `paused` — cost budget exhausted |
| Completion gate | **failed** (correctly — see below) |

Bounded on both sides: cogos' own `Budget`, plus `--max-budget-usd 2.5` per CLI call.

## The headline result

**The mission did not complete, and the completion gate never produced a false completion.**

Across 11 cycles and $20.51, the gate refused every time and named exactly what was missing:

```
failed  success_criteria    'calc.py exists ... and defines add_percent(...)' (no passing
                            verification record bound to it)  [and 4 more]
failed  contradictions      ctr_…184 (severity 1.00), ctr_…198 (0.94), ctr_…524 (0.84), …
failed  required_artifacts  missing or unverified: /tmp/cogos-live-run/calc.py,
                            /tmp/cogos-live-run/test_calc.py
passed  tests, blocked_operations, human_requests
skipped artifact_integrity  no verified artifacts to re-check
```

### The nuance that matters

The executive **did** write both deliverables, and they are **correct**. Verified independently
after the run:

```
$ .venv/bin/python -m pytest -q test_calc.py
....                                                       [100%]
4 passed in 0.01s
```

`calc.py` implements `add_percent(value, percent)` with type hints, a docstring and
`round(value * (1 + percent / 100), 2)`; `test_calc.py` covers positive, zero, negative and
rounding cases.

So the gate was **conservative, not wrong**. It refused because cogos never ran its own
verification of those files — they were written to disk but never registered as verified
artifacts, so no receipt was ever bound to any criterion. Given the choice between claiming a
completion it had not verified and refusing one it had actually achieved, it refused. That is
the correct direction to fail in, and it is what F2–F5 were repaired to guarantee.

It is also a real shortcoming: the mission spent its budget on epistemics — falsifying its own
leading belief, auditing the provenance of a mission constraint, challenging the corroboration
strength of a five-line requirements file — and never got round to the verification step that
would have closed work it had already done correctly.

## What this validates live

| Property | Result |
| --- | --- |
| Adapter command shape against CLI 2.1.267 | works; structured output parses |
| **Model residency** | **verified on every cognition call** (11 in segment 1, 8 recorded in segment 2); the `claude-haiku-4-5` auxiliary model was correctly excluded from the residency decision, and the executive was never downgraded |
| **Completion integrity** | no false completion in 11 cycles |
| **Pause and resume from durable state** | segment 2 resumed at cycle 6 with $8.81 already spent and continued to cycle 11 — mission state, not conversation, carried the run |
| Budget guards | fired correctly: wall-clock first, then cost |
| Failure diagnosis and replan | a task failed on an operation-kind mismatch; the runtime classified it structural and replanned rather than retrying |
| Meta-cognitive controller | at cycle 4 it observed "four cycles and nine tool calls have produced zero deliverables" and switched from inspection to writing code |
| Learned retrieval policy | selected on 5 decisions, correctly using the validated baseline throughout (below the data-support threshold) |

## What this does **not** validate

- **The reality anchor (R1) never ran.** It triggers before a completion attempt, and the
  mission never reached one. R1 remains verified locally only.
- No skill was proposed or promoted, so L6 saw no live exercise.
- One live run of one coding mission is not a measurement of anything. There is no comparison
  against a baseline, no repetition, and no held-out task family.

## Defects this run found

Both were invisible to the offline suite and are fixed with regression tests.

**1. A billed failure recorded with an empty error** (`f547fee`). A `challenge` call returned
the CLI's `is_error` with an empty `result`; cogos recorded `error_kind=structural`,
`error=""` — after being billed $0.25 for 1,903 output tokens. The trace that cost the most
explained the least. The adapter now reconstructs a diagnostic from the fields the CLI does
return and never leaves the error blank on a failure.

**2. A resolved contradiction that could not be recorded as resolved** (`f39d4f3`). The
executive correctly reasoned that "calc.py and test_calc.py do not exist" and "both deliverables
now exist on disk" were each true of their own period, and wrote out the resolution: *"both are
true within their periods; no live contradiction remains."* But `ContradictionSpec` had no field
for resolving anything, so that resolution could only be appended as another record at severity
0 while the original severity-1.0 contradiction stayed open. The controller then re-issued
`must_falsify` against a settled dispute on cycles 5, 7, 8 and 9 — roughly $2 a cycle spent
re-litigating a question the model had already answered correctly. `ContradictionSpec` now
carries `resolves_contradiction_ids` and a `resolution`, accepted only with a stated reason.

## Known limitation this run exposed

**Budget guards are cycle-boundary checks, not hard interrupts.** They are evaluated at the
start of a cycle, so a single long cycle overshoots: wall clock stopped at 2940s against a 2400s
cap, and cost at $20.51 against a $19.00 cap. `--max-budget-usd` bounds each individual CLI
call, which is what keeps the overshoot to roughly one cycle rather than unbounded — but a
caller should treat a cogos budget as "stop somewhere after this", not as a ceiling. Making it a
true ceiling needs the reservation path (`ResourceLedger.reserve`) consulted before dispatch
rather than only at the cycle boundary.
