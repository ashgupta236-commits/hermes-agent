# CogOS upgrade status — F1–F7, R1–R5, L1–L6

Branch `claude/autonomous-cognitive-os-og6r0p`, from audited commit `88da4ac`.

This report tracks three states separately, as the brief requires:

| State | Means |
| --- | --- |
| **implemented** | reachable from the real runtime, not a schema, mock, README or disabled method |
| **verified locally** | the relevant local checks were actually run, and are named below |
| **validated live** | a real supported provider or environment run supplied the evidence |

A live run against the real `claude_code` adapter **has now been performed** — 11 cycles, 24
model calls, $20.51, Claude Code CLI 2.1.267, model `claude-fable-5-1`. Full write-up in
[`evidence/LIVE_RUN.md`](evidence/LIVE_RUN.md).

It validates live: adapter command shape and structured-output parsing; **model residency
verified on every cognition call** with no downgrade; **completion integrity — no false
completion across 11 cycles**; pause and resume from durable state; budget guards firing;
failure diagnosis and replan. The mission itself did **not** complete: it wrote both
deliverables correctly (independently confirmed, 4/4 tests pass) but never ran its own
verification of them, so the gate refused for want of a bound receipt. Conservative, not wrong —
and a real shortcoming, since it spent its budget on epistemics instead of closing work it had
already done.

It does **not** validate the reality anchor: R1 triggers before a completion attempt, and the
mission never reached one. Rows below say `validated live` only where that run actually supplied
the evidence. Everything else remains local-only, and where a live run is the only thing that
could settle a question, that is stated in the row rather than substituted for.

---

## 1. Commits and changed files

| Commit | Scope |
| --- | --- |
| `7d7a14b` | F1–F7 repairs |
| `9df3df9` | R1 reality anchoring |
| `cbd0dac` | R2–R5 observability, continuity, discovery, boundaries, recovery |
| `95c6d7d` | L1–L3 experience learning kernel |
| `447ffc2` | L4–L6 elicitation, research direction, prediction discipline, evolution |

New modules: `cogos/schemas/anchor.py`, `cogos/schemas/channels.py`,
`cogos/schemas/experience.py`, `cogos/verification/reality_anchor.py`,
`cogos/executive/anchor_service.py`, `cogos/observability/channels.py`,
`cogos/adapters/continuity.py`, `cogos/evaluation/skill_runner.py`, `cogos/learning/` (five
modules). Modified: the executive loop, verification engine, mission and world schemas, the
Claude Code adapter, resource ledger, planner, capability firewall, tool fabric, skill compiler
and evaluation scenarios.

## 2. Test commands actually executed

```
.venv/bin/python -m pytest tests/cogos -q          # 425 passed
make cogos-lint                                    # ruff: All checks passed
make cogos-typecheck                               # ty:   All checks passed
make cogos-eval                                    # 19/19 scenarios passed
make cogos-demo                                    # offline demo reaches a verified completion
python <scratch>/reproduce_cogos_audit.py .        # all seven findings repaired
```

New inspectable test evidence, by file:

| File | Tests | Covers |
| --- | --- | --- |
| `tests/cogos/test_audit_findings.py` | 12 | F1–F7 |
| `tests/cogos/test_reality_anchor.py` | 20 | R1 acceptance items 1–8 plus end-to-end hold |
| `tests/cogos/test_runtime_integrity.py` | 18 | R2–R5 |
| `tests/cogos/test_learning.py` | 25 | L1–L3 |
| `tests/cogos/test_learning_l4_l6.py` | 27 | L4–L6 |

`docs/cogos/evidence/a20-end-to-end.txt` holds the raw A20 run output.

## 3. Historical reproduction

The brief's pinned script was run against `88da4ac` **before any change**. All seven
observations matched the historical record exactly, so every finding was reproduced rather than
assumed. Re-run after the repairs, all seven show the required behaviour. The F1 case needed the
setup adapted — `procedural_runner` no longer exists — and the behavioural counterexample was
preserved: the same non-executing candidate, now ineligible.

| Case | At `88da4ac` | Now |
| --- | --- | --- |
| `wrong_target_receipt` | `passed` | `failed` |
| `deleted_verified_artifact` | `passed` before and after deletion | `passed` before, `failed` after |
| `referenced_receipt_eviction` | receipt evicted, gate `failed` | receipt resolves, gate `passed` |
| `nonexecuting_skill_promotion` | 0.5 → 0.7, promoted | 0 measured cases, not promoted |
| `unsupported_judge_attestation` | satisfied, gate `passed` | not satisfied, gate `failed` |
| `model_identity_validation` | mixed/older/absent all accepted | mismatch, mismatch, unknown |
| `retry_usage_undercount` | 1 call, $2 of $3 | 2 calls, $3 of $3 |

## 4. Behaviour repaired for F1–F7

**F1 measured skill evaluation.** The production runner scored candidates by text overlap
between prose steps and the case description. `MeasuredSkillRunner` executes each case as a real
task in a fixture workspace through the real tool fabric, under the same capability firewall as
real work, and scores from the filesystem and test output. Promotion requires measured execution
on at least two scored cases. A non-executing procedure scores 0 and is refused; an executable
one measures 1.00 against a 0.20 baseline and promotes — the gate rejects the unmeasured without
making promotion unreachable.

**F2 grounded judgment.** A confident judgement with named checks could satisfy a criterion with
no artifact, test, evidenced claim or verified task anywhere in state. Judgements are now checked
against material the runtime can independently re-check; without any, the criterion is recorded
inconclusive and undecidable.

**F3 artifact integrity.** `verified` was a claim about the past. Artifacts record the hash they
were verified at; the completion gate re-hashes every verified artifact and refuses stale
evidence. The loop responds by re-verifying changed artifacts and withdrawing criteria that
rested on the old bytes, so a regenerated file is re-checked rather than blocking forever.

**F4 receipt binding.** Citation binds on target id and is validated at insertion; the gate
independently requires a bound, passing record.

**F5 durable history.** Retention preserves every referenced receipt and bounds only unreferenced
records.

**F6 model identity.** Residency is now verified / mismatch / unknown. A mixed model set and an
older generation of the same family are mismatches; an alias resolution and a date-stamped id
still match; absent telemetry is `unknown`, recorded as such rather than as a verified run.

**F7 complete accounting.** Every billed attempt is carried on the response and charged to the
ledger, and the ledger can reserve budget for a call in flight.

## 5. Acceptance matrix A01–A20

| ID | Requirement | Implemented | Verified locally | Validated live | Notes |
| --- | --- | --- | --- | --- | --- |
| A01 | F1 measured skill evaluation | yes | yes | no | Real execution both sides; fake candidate ineligible, useful candidate promoted. Unchanged-candidate uplift is covered by the improvement threshold, not by a dedicated fixture. |
| A02 | F2 grounded judgment | yes | yes | no | Unsupported judgement inconclusive; the same judgement accepted when a real artifact backs it. |
| A03 | F3 artifact integrity | yes | partial | no | Mutation and deletion covered. **Not covered:** an inaccessible *delivery* target (no delivery channel exists in this runtime) and a verification/commit race, which needs a concurrency fixture. |
| A04 | F4 receipt binding | yes | partial | no | Wrong target rejected at citation and at the gate. **Not covered:** rubric- and revision-scoped binding — there is no rubric object, and receipts are not yet bound to a state revision. |
| A05 | F5 durable history | yes | partial | no | Referenced receipts survive volume pressure and restart. **Not covered:** an interrupted write mid-append. |
| A06 | F6 model identity | yes | yes | **yes** | Mismatch, unknown and alias resolution distinguished. Live: residency verified on every cognition call of the 11-cycle run, auxiliary haiku correctly excluded, executive never downgraded. |
| A07 | F7 complete accounting | yes | partial | no | The retry fixture records two attempts and $3. **Not covered:** concurrent calls sharing a ledger, and deadline enforcement. Unknown cost is recorded as 0 and not distinguished from a real zero. |
| A08 | R1 independent input | yes | yes | no | The packet excludes the executive's conclusion by construction and by check; units, environment identity, timestamps and the omission manifest travel with it. Isolation is recorded, including what it does not establish. |
| A09 | R1 enforced holds | yes | yes | no | Refutation, timeout, malformed and unverifiable-isolation verdicts all hold; holds block a task queued before them, survive a reload, permit read-only evidence gathering, and clear only on new evidence within two rounds. |
| A10 | R2 behaviour records | yes | yes | no | Four channels recorded and reconciled across a real run; forged self-reports (claimed checks with no execution) and missing telemetry are visible; the chain detects an edited record. |
| A11 | R3 continuity | yes | yes | **partial** | The headless adapter reports `external_state_only` with its cost stated; handoff completeness is checked; the anchor's policy is the inverse of the executive's. **Not covered:** native session continuation, because no adapter here supports it. Live: the headless adapter ran 24 calls as `external_state_only` and the mission resumed from durable state across a pause. |
| A12 | R4 tools and boundaries | yes | partial | no | Discovery works and is not authorization; a denied destination is now denied on the shell route too. **Weaker than it sounds:** boundary enforcement is argument analysis, not OS-level sandboxing — an allowed interpreter can still write wherever its process can. Specialist-subprocess effects are not separately audited. |
| A13 | R5 recovery | yes | partial | **partial** | Restart preserves holds, spent rounds, denied grants, attempt budgets, resource totals and contradictions; held branches do not stop unrelated work. **Not covered:** idempotency keys and effect-checking before replay of an interrupted *external* operation. Live: a paused mission resumed at cycle 6 with $8.81 already spent and continued to cycle 11 without repeating completed work. |
| A14 | L1 outcome data | yes | yes | no | Pre-action features only (asserted structurally); receipts must resolve; invalidated receipts traceable through lineage; unknown/censored kept but excluded. |
| A15 | L2 learning | yes | yes | no | Hand-calculated SARSA transition checked term by term; parameters persist across restart; the bandit's estimates change the runtime's next retrieval. **Real-task gain: not measured.** No comparative evaluation of missions with and without the learned policy has been run. |
| A16 | L3 replay and retention | yes | partial | no | Split by task instance, duplicates collapsed and counted, on-policy restriction, sampling tracked, retention measured before/after. **Not covered:** option discovery evaluated on fresh instances — options are still the skill compiler's candidates. |
| A17 | L4 research and elicitation | yes | partial | no | Falsifying experiments ranked by information per unit resource; disconfirmation revises and promotes alternatives; unproductive branches stop; each failure class gets a matched intervention and refusals are never routed around. **Not covered:** a matched-budget evaluation showing the elicitation policy improves verified outcomes on held-out failure cases. |
| A18 | L5 memory and world model | partial | partial | no | Predictions are logged before outcomes with the stated probability preserved; consolidation retains contradictory evidence. **Not implemented:** a versioned candidate memory store with reversible switch-over — consolidation still mutates in place. |
| A19 | L6 controlled evolution | yes | yes | no | Real candidate lineage, contract fingerprint binding, defective candidate rejected, unrun candidate rejected, transactional activation and rollback to a real destination. Production promotion is deliberately blocked while the holdout is developer-visible. |
| A20 | End-to-end release | yes | yes | no | One coding mission and one research mission complete locally with exact artifacts, bound criteria receipts, attempts, resources and unresolved limitations — see §6. |

## 6. A20: two complete local missions

Raw output: `docs/cogos/evidence/a20-end-to-end.txt`.

**Coding mission** — "Build the feature described in REQUIREMENTS.md." Status `complete`,
completion gate `passed` on all seven checks. Two artifacts (`calc.py`, `test_calc.py`) verified
with content hashes that still match at the gate. Both success criteria satisfied with receipts
bound to those criteria. One passing test record. The reality anchor ran, cited real observation
ids, and cleared.

**Research mission** — "Research whether to enter market Y with product X and decide." Status
`complete`, gate `passed`. Contradictory sources were reconciled by scope and period rather than
averaged. The anchor returned `inconclusive`: the deterministic offline anchor can adjudicate
mechanically checkable propositions about artifacts and tests, and a decision-quality
proposition is not one of those. That is recorded as an uncorroborated limitation on the run —
it did not block completion, and it did not silently pass either.

Both are **scripted demonstrations**: the specialist policies are deterministic fixtures, not a
model. They validate the plumbing end to end and nothing about model behaviour.

## 7. Live validation

A live run **was** performed — see [`evidence/LIVE_RUN.md`](evidence/LIVE_RUN.md) for the full
account, including the two defects it found (a billed failure recorded with an empty error, and
a resolved contradiction that had no channel to be recorded as resolved), and the limitation it
exposed: budget guards are cycle-boundary checks, not hard interrupts, so a long cycle overshoots
the cap by roughly one cycle.

Reproduce it with:

```bash
.venv/bin/python -m cogos --adapter claude_code --model claude-fable-5-1 \
  mission new "Build the feature described in REQUIREMENTS.md." --run
```

Budget it explicitly: the run above cost $20.51 for 11 cycles without completing. Capability
detection still must not launch a paid run on its own.

Three things remain specifically **not** established, and the live run did not settle any of
them:

1. whether blinding reduces a real model's bias — R1 never ran live, because the mission never
   reached a completion attempt, and the offline anchor is deterministic;
2. whether the isolation boundary holds against a real headless process, which may still inherit
   project context (`CLAUDE.md`, skills, MCP configuration);
3. whether the learned retrieval policy improves real missions — it stayed on its validated
   baseline for all five live decisions, correctly, being below the data-support threshold.

## 8. Learning: what actually changed

- **Experience stored:** yes. Versioned records with pre-action features, receipts and lineage.
- **External agent changed:** yes. The contextual bandit's persisted statistics change which
  retrieval strategy the running loop selects, and a test drives that flip through the real
  runtime.
- **Model parameters trained:** **no**, and not possible here. The hosted model's weights are
  untouched. Nothing in `cogos/learning/` claims otherwise.
- **Real-task improvement measured:** **no.** The learner works; whether it helps is an open
  question requiring a paired comparative evaluation that has not been run.

## 9. Migration and rollback

No database migration is required. New mission-state fields (`observations`,
`evidence_snapshots`, `belief_snapshots`, `anchor_assessments`, `disagreements`, `holds`,
`resolution_receipts`, `Artifact.verified_hash`/`verified_at`,
`Task.verification_attempt_ids`, `Prediction.made_at`/`resolved_at`/`stated_probability`) all
default to empty or `None`, so a mission written by the previous version loads unchanged.

One behavioural consequence is worth stating: an artifact verified before this change has no
`verified_hash`, so the new integrity check treats it as unverified and the loop re-verifies it
on the next cycle. That is the intended direction — an artifact whose verified bytes were never
recorded cannot be shown to be intact — but it means an in-flight mission will re-run artifact
verification once after the upgrade.

Rollback is `git revert` of the five commits, in reverse order, or a checkout of `88da4ac`. New
state fields are ignored by the older code. Learned policies and the evolution registry live
under separate store keys (`learned_policies`, `evolution_registry`, `decision_chain`) and are
inert to the older runtime.

## 10. What this evidence does not show

This is not a claim of AGI, of autonomous weight learning, or of reliable recursive
self-improvement, and the architecture containing labels for those things is not evidence of
them. The defensible milestones here are narrower and, I think, real: completion that cannot be
claimed without resolvable bound receipts; artifacts that cannot pass a gate after being deleted
or edited; a second reading of the evidence the executive cannot edit or clear; accounting that
survives retries; a learner whose parameters move and whose decisions change; and a candidate
path that rejects a defective candidate and can roll back a bad release.

Broader claims would need a declared evaluation framework, an independent assessment, and
replication — none of which this repository provides.
