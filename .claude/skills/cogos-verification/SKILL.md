---
name: cogos-verification
description: Verification requirements by artifact type (code, research, data, decisions, UI) and the completion-integrity gate — use before marking any task, criterion, or mission complete.
---

# Verification engine rules

Creation and verification are separate steps. Evidence of correctness must be proportional to
importance; "looks right" is never sufficient.

| Artifact | Required verification |
|---|---|
| Code | unit/integration tests run (`run_tests`), lint (`ruff`), type check (`ty`), runtime checks; property tests where useful |
| Research | source verification, independence (≥2 roots for high confidence), primary preference, freshness, claim/source consistency, no unresolved contradiction |
| Data | schema validation, independent recalculation, anomaly checks, reconciliation totals |
| Decisions | assumption sensitivity (simulation), counterfactual alternatives, independent critic when stakes justify |
| UI | rendered inspection where tooling permits (Playwright/screenshot) |

## Completion gate (runtime-enforced)
`mission_completion_check` refuses completion unless: every success criterion is satisfied with a
verification record; the latest record of every test command passed; no unresolved contradiction
with severity ≥ 0.5; no unresolved blocked operation on unfinished tasks; no unanswered human
request with no independent work remaining; every required artifact exists and is verified.

## Never
Delete, weaken, skip or hard-code around a legitimate test to obtain a pass. A failing test is a
diagnosis target, not an obstacle. Reproduce first, then fix the cause, then re-verify.

## Commands
`.venv/bin/python -m pytest tests/cogos -q` · `.venv/bin/ruff check cogos` · `.venv/bin/ty check cogos`
`python -m cogos eval --suite all` (acceptance A–J + adversarial suite)
