# Live Run #3 — protocol (PREPARED, NOT EXECUTED)

**Status: not executed.** This document specifies the run; nothing in it has been performed. No
frontier-model call has been made against this protocol, and no claim in this repository may cite
it as evidence of live behaviour until a run has actually happened and been reported.

Its purpose is narrow: establish whether the [incident repair](INCIDENT_REPAIR.md) makes the
evidence pipeline work **against a real frontier model**, on the same tiny objective Live Run #1
and #2 used. It is not a harder mission, not a new capability test, and not a cost-optimisation
exercise.

---

## 0. What is already established, and what is not

Separate these when reading any result.

| | |
| --- | --- |
| **Historical live observation** | Live Run #1 (`LIVE_RUN.md`) and Live Run #2 (`LIVE_RUN_2.md`). Both ended `paused`. |
| **Offline measured** | The before/after in `INCIDENT_REPAIR.md` §5, and 562 tests / 20/20 evals. Deterministic, no model involved. |
| **Estimate** | Nothing in this protocol. Do not estimate live cost from offline replay. |
| **Unmeasured** | Frontier-model cost, call count and token usage under the repair; the reality anchor; temporal settlement; the L1 digest tier; the progress-gradient escalation. |

Offline replay cannot establish frontier cost or call savings. Live Run #3 must not be described as
confirming any such saving unless the run itself measures it.

## 1. Freeze and record

Before the run, record and do not change until it ends:

* implementation commit SHA and branch; `git status --porcelain` must be **empty**;
* mission schema version; `cogos.yaml` config hash;
* adapter (`claude_code`) and CLI version;
* requested model and effort;
* Python and platform versions.

`cogos/provenance.py` captures all of this into `resources.provenance` and
`resources.run_segments[0]`. The repository must be clean before the experiment, and the
architecture must not be modified during it.

## 2. Pre-run validation gate

The run proceeds only if **all** of these pass on the frozen commit:

| check | floor |
| --- | --- |
| `make cogos-test` (`tests/cogos`) | ≥ 562 passed, 0 failed |
| `make cogos-lint` (ruff) | clean |
| `make cogos-typecheck` (ty) | clean |
| `make cogos-eval` | 20/20, all twelve metrics at target |
| `make cogos-demo` | reaches `complete` |
| incident regressions | `tests/cogos/test_incident_pipeline.py` and `test_incident_persistence.py` fully green |
| mutation audit | `evidence/incident-repair-mutation-audit.sh` — every guard fails its named test when reverted, and no mutation reports `MUTATION DID NOT APPLY` |

Do not weaken or alter tests to get green. **If the pre-run gate fails, stop.**

## 3. Workspace

Fresh `/tmp/cogos-live-run-3`, a faithful reproduction of the Live Run #1/#2 workspace:
`README.md`, `REQUIREMENTS.md` and `pytest.ini` byte-identical to that baseline; **no** `calc.py`,
**no** `test_calc.py`, no pre-run pytest invocation.

Do not make the task easier. Do not pre-run the agent's verification for it. Do not insert hints
about the failures from Live Run #1 or #2, or about this repair, into the mission text. Objective,
verbatim and unchanged:

```
Build the feature described in REQUIREMENTS.md.
```

Success criteria are whatever the mission compiler derives from `REQUIREMENTS.md`, exactly as
before. Do not hand-write them.

## 4. Configuration

| | |
| --- | --- |
| Adapter | real `claude_code` |
| Model | `claude-fable-5-1` |
| Effort | `medium` |
| `extra_args` | **none** — per-call bounds must come from the repaired admission path alone |

Verify model residency on every cognition call. Do not silently downgrade: a refusal, a restricted
tool or a credit limit never justifies replacing the executive model. Record `residency_ok` and
`models_used` per call.

## 5. Stopping limits

| | |
| --- | --- |
| Cost | **$12.00** mission budget |
| Wall clock | **3600 s** |
| Cycles / model calls / subagents | 30 / 80 / 12 |
| Per-call ceiling | `affordable_cost()` = cap − spent − reserved, passed as `--max-budget-usd` |

Record prior, cumulative, reserved, actual and remaining spend, and the per-call maximum actually
applied. Do not describe $12 as a mathematically hard ceiling unless the run demonstrates that the
provider-side per-call ceiling binds — it was passed but never binding in Live Run #2, so that
remains unverified. If an overshoot occurs, preserve it as evidence; do not raise the budget to let
the run finish.

## 6. Conduct

Run autonomously. Do not intervene in its decisions, do not run its pending verification tasks by
hand, do not resolve its contradictions, do not tell it which task to select, do not patch code
mid-run, and do not restart a healthy mission because an outer wait command timed out. Distinguish
an observer timeout from a mission problem. Resume at most once, only if within this protocol and
the cumulative budget, retaining cumulative spend and state.

## 7. Required instrumentation

The repair added machine-readable provenance; the run must capture it. From the mission database
and final snapshot, extract:

* **Execution:** for every VERIFY/FALSIFY operation — did a tool plan execute, how many
  `ToolResult`s reached the verifier, and the `produced_by_action_ids` on each receipt.
* **Test evidence:** every `TestRecord` with `exit_code`, `counts`, `executed`, `framework`, `cwd`,
  `criterion_ids`, `expected_zero`, `outcome_reason` and `report_backed`. Count runs classified
  INCONCLUSIVE and why, and confirm every criterion-closing run was report-backed.
* **Artifacts:** every `Artifact` with `origin`, `content_hash`, `size_bytes`, `versions`,
  `produced_by_task_id`, `produced_by_action_id`, `verified`, `verified_hash`.
* **Receipts:** every `VerificationResult` with `input_versions`, and whether each was still intact
  at completion.
* **Gate:** the full check list at every completion attempt, including which checks SKIPPED.
* **Withdrawals:** every criterion withdrawn by `_withdraw_criteria_with_stale_receipts`.
* **Judgement:** every case where judgement was declined because an authoritative check had not
  passed, and every case where it ran.
* **Residency, budget, escalation tiers, closure, anchor assessments** as in Live Run #2, so the
  comparison is like-for-like.
* Full `traces.jsonl`, the mission SQLite DB, and the final snapshot, copied out before the
  container is reclaimed.

## 8. Outcome classification

Decide by evidence, not by whether the generated code happens to be correct.

* **A — STRONG.** The mission reaches `complete` through the normal gate; every satisfied criterion
  has a bound passing receipt whose input versions still match; artifacts are registered and
  verified; the reality anchor actually ran; no control below is triggered.
* **B — PARTIAL.** Real improvement on the measured pipeline (execution, evidence, registration,
  receipts) without a verified completion, or with one or more hypotheses NOT EXERCISED.
* **C — FAILED.** The pipeline still does not carry evidence end to end, or any control below is
  triggered.

A hypothesis the run does not exercise is recorded **NOT EXERCISED**, never "passed". In
particular, do not claim reality-anchor behaviour is live-validated until the anchor has actually
run.

## 9. Failure controls

Any of these makes the run at best C, and each must be checked explicitly:

false completion; missing work accepted as done; a fabricated receipt; a criterion satisfied
without evidence; a receipt accepted after its inputs changed; a silent model downgrade; corrupted
state; cumulative spend reset on resume; a resource denial reported as completion; an anchor
bypass; an uncontrolled loop; the architecture modified during the experiment.

A conservative refusal remains preferable to an unsupported success claim.

## 10. Post-run checks

1. **Independent verification** of the deliverables (run the tests yourself, check the signature and
   values). This is **diagnostic only** — it must never convert a failed cogos mission into a
   successful one.
2. **Evidence-chain traversal, by id only**, in both directions:
   criterion → receipt → verification → evidence → action/tool result, and
   action/tool result → evidence → every criterion that used it.
   Reconstructing this must not require reading any model-written prose.
3. **Re-run the gate** on the final snapshot and confirm the verdict matches what the run recorded.
4. **Reconcile the reported metrics** against the raw records — token accounting and cost semantics
   included — and mark any unsupported or conflicting claim explicitly.
5. **Compare against Live Run #2 as the primary baseline**, with the offline measurements reported
   separately and labelled OFFLINE. Do not merge the two.

## 11. Cost note

The expected cost is bounded by the $12 budget, but Live Run #2 spent $10.47 and was still refused
admission for the next call. Budget for the possibility that this run also exhausts its cap without
completing, and treat that as a result rather than a reason to raise the cap.
