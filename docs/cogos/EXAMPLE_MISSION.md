# Worked example: a research mission end to end

This walks one real mission through the runtime, showing what the human supplies and what the
system decides. Commands are copy-pasteable; the outputs are from an actual run.

## 1. The human supplies an objective — nothing else

```bash
.venv/bin/python -m cogos mission new \
  "Investigate whether a small B2B SaaS company selling invoicing software should enter the Saudi Arabian market in 2026."
```

```
mission msn_1m24fe1hy5076c2ed compiled (decision): 16 tasks, 7 criteria, 9 unknowns
```

The Mission Compiler decided, without being told, that this is a **decision** mission and produced:

- **Success criteria** it can check later, each with a verification method — for example a decision
  memo with an explicit recommendation and the top three conditions that would flip it; a regulatory
  checklist traced to primary sources; a quantitative model computed with a deterministic tool.
- **Unknowns scored by decision value** — the company's own profile, which e-invoicing waves apply
  in the entry window, entry cost, competitive density, binding constraints.
- **Competing hypotheses** rather than one answer: defer/partner, go niche-direct, or stage through
  a neighbouring market first — each with unique predictions.
- **A task DAG** mixing `retrieve_memory`, `inspect_files`, `search`, `instantiate_specialist`,
  `calculate`, `falsify`, `verify` and `synthesize`, with dependencies.
- **Two human requests** for the only genuinely non-inferable facts: the company's own financials
  and what "enter in 2026" means contractually. Note these do **not** block the mission — every
  independent investigation still runs.

## 2. The system runs itself

```bash
.venv/bin/python -m cogos run msn_1m24fe1hy5076c2ed
```

Each cycle: perceive events → update world model and beliefs → assess → choose the
highest-expected-value step → act → interpret the result into state → attribute success or failure
→ learn → persist → replan. Watch it live with `-v`, or afterwards:

```bash
.venv/bin/python -m cogos trace msn_1m24fe1hy5076c2ed --kind select
.venv/bin/python -m cogos explain msn_1m24fe1hy5076c2ed
```

Things the human never had to specify, visible in the trace: which unknown to research first, when
to spawn a researcher versus reason directly, when to stop searching, when a contradiction between
two market-size figures needed a scope investigation rather than an average, when to challenge the
leading conclusion independently, and when to verify before claiming a criterion.

## 3. The human answers only what cannot be inferred

```bash
.venv/bin/python -m cogos status msn_1m24fe1hy5076c2ed        # lists open human requests
.venv/bin/python -m cogos answer msn_1m24fe1hy5076c2ed hreq_… "ARR ~$2M, 6 engineers, no Arabic support yet"
.venv/bin/python -m cogos run msn_1m24fe1hy5076c2ed           # continues from durable state
```

Corrections and new information use the same channel and reactivate a blocked mission:

```bash
.venv/bin/python -m cogos correct msn_… "We already have a partner in the region."
.venv/bin/python -m cogos correct msn_… "Board moved the deadline to Q3." --kind information
```

An action the firewall classes as needing authorization is requested explicitly and never assumed:

```bash
.venv/bin/python -m cogos authorize msn_… financial
```

## 4. Completion is earned, not declared

The mission reaches `complete` only when the completion gate passes: every criterion satisfied with
a verification record, the latest run of every test command passing, no unresolved contradiction
above the severity threshold, no blocked operation on unfinished work, and every required artifact
verified. Otherwise the honest terminal state is `blocked_external` (with exactly what would unblock
it) or `failed` — never a confident answer dressed as completion.

## 5. Restarting costs nothing

```bash
.venv/bin/python -m cogos boot      # reconstructs everything from .cogos/, names the resume target
.venv/bin/python -m cogos resume
```

The human is never asked to restate what happened. A `SessionStart` hook prints this report
automatically in a fresh Claude Code session.

## The implementation counterpart

`python -m cogos demo` runs the same loop over an implementation mission in a throwaway workspace
(offline, ~5 seconds): it reads a requirements file, chooses an approach, delegates the
implementation, runs the real test suite, verifies, synthesises, and passes the completion gate —
then proposes (but does not promote) a candidate skill from the successful trajectory.
