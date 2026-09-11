# Trust-boundary investigation and repair

**Live Run #3 remains BLOCKED. It was not executed.**

This document reports a bounded investigation into whether the [incident repair](INCIDENT_REPAIR.md)
actually closes the evidence pipeline, and the repair that followed. It supersedes several claims in
that document; the corrections are listed in §9 and appended there rather than edited into it.

| | |
| --- | --- |
| Starting SHA | `25ec1400831c174add64af2cea1510e50be10670` (branch `claude/autonomous-cognitive-os-og6r0p`, tree clean) |
| Ending state | this commit, same branch |
| Live Run #3 | **not executed**, and **BLOCKED** — see §10 |

---

## 1. The claim that was refuted

The previous repair rested on one sentence:

> A report at a path the engine chose cannot be produced by a runner that never ran.

**That is false, and it is false twice over.** The path is handed to the child in its own argv, and
the report's contents were never compared against anything. Reproduced end to end against the real
pipeline (`build_default_fabric` → `verify_code` → `verify_criterion` → `mission_completion_check`)
over a workspace whose `add_percent(100, 10)` returns `999.0`:

```
workspace pytest.py that reads --junitxml= from sys.argv and writes passing XML
  1 report accepted   : True        2 verification : passed
  3 criterion         : True        4 gate         : PASSED — "all completion gates satisfied"
  GROUND TRUTH add_percent(100, 10) = 999.0
```

Twelve distinct workspace-controlled fixtures reached the same four outcomes. The generalisation
matters more than any of them:

> **Any process that imports the code under test can author that process's own report, stdout and
> exit status.**

Each hardening property that might seem to fix this was measured and defeated:

| proposed property | why it fails |
| --- | --- |
| random report filename | the name is in the child's argv |
| nonce passed to the child | anything passed to the child is readable by the child |
| report hash | the engine cannot hash a file it did not author |
| trusted launcher script | the launcher must ultimately import workspace code |
| exit code | `os._exit(0)` in any hook |
| file ownership / permissions | the child runs as the same uid |
| running the runner from outside the workspace | the workspace `conftest.py` is still imported |
| engine-owned plugin loaded via `-p` | the workspace unregisters it via `config.pluginmanager` |

And one fixture defeats all of them simultaneously: a `conftest.py` hookwrapper on
`pytest_runtest_makereport` flips every failure to a pass, after which the **genuine** runner writes
a **genuine**, well-formed, correctly located, freshly stamped report whose contents are false.
Report integrity, runner identity, freshness and file provenance all hold, and the result is still
fabricated.

## 2. The trust boundary, as it actually is

| component | who controls it |
| --- | --- |
| the interpreter, flags, config file, plugin loading, environment, import path, cwd, evidence destination | **the engine**, since this repair |
| the test selection | the plan, validated against the tree and checked back against the report |
| the fact that a process started, terminated, and with what status | **the engine** (`PROCESS_OBSERVATION`) |
| `conftest.py`, plugins, test modules, the implementation | the workspace |
| everything the test process does after `exec` — pytest internals, the plugin registry, result objects, exit behaviour, the report file | **the workspace** |
| the evidence store, receipt creation, criterion evaluation, the completion gate | the engine, in its own process |

The last workspace row is the boundary problem, and no arrangement of the rows above it fixes it.

## 3. The design chosen, and why

Of the three defensible designs, this repair takes **option 3: narrow the threat model explicitly,
classify same-process adversarial verification as unsupported, and fail closed for completion.**

Option 1 (drive the subject through a protocol in a contained child) and option 2 (an OS-enforced
boundary) both reduce to containment this runtime does not have: same uid, same filesystem, no
namespace or seccomp primitive in scope. Building one is a sandbox platform, which this task does
not do. Claiming either without the containment would be the same kind of overclaim being corrected.

So the repair has three layers, and each is described as what it is:

**(a) A trusted verifier path** — `cogos/verification/attestation.py::build_trusted_run`. The engine
stops executing the plan's command and builds the invocation itself: its own interpreter, `-P`,
`-p no:cacheprovider`, an engine-authored `pytest.ini` outside the workspace via `-c`, an explicit
`--rootdir`, a scrubbed environment (`PYTHONPATH`, `PYTEST_ADDOPTS`, `PYTEST_PLUGINS`, … removed;
`PYTHONSAFEPATH=1`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` set), **no shell**, and an evidence
destination in a `0700` directory created fresh per run. Measured: this stops the `pytest.py`
shadow, `addopts = -p <plugin>` and ambient `PYTEST_ADDOPTS` **at the source** — the real runner runs
and reports the real failures. It is *not* a boundary, and the module says so.

**(b) A differential control** — `attestation.py::differential_control`. The engine copies the
workspace to a private directory, replaces every non-test Python module with a stub that still
imports and still exposes every name but raises on use, re-runs its own invocation there, and
requires that **every** identity that passed for real is present in the control report and **none**
of them passes. A result that survives the implementation being withheld was never a measurement of
it. This catches every forgery above, including the hookwrapper. It is a **sensitivity check, not
containment**: an adversary who models it and answers the two runs differently defeats it.

**(c) Fail closed** — `governance.trust_workspace_code`, default **False**. With it False an
in-process test result is capped at `PROCESS_OBSERVATION` and `verify_code` returns INCONCLUSIVE,
so no criterion closes and the gate refuses — *however green the run and however honest the
workspace*. Setting it True is a human's statement that a particular workspace's code is not
targeting the verifier. It never disables (a) or (b); it only lets their result carry authority.

### Evidence authority

```
UNTRUSTED_SELF_REPORT  <  PROCESS_OBSERVATION  <  TRUSTED_HARNESS  <  INDEPENDENT_VALIDATION
```

* `UNTRUSTED_SELF_REPORT` — stdout, an exit code, a JUnit file written by a process that imports the
  subject. **Also what every absent or unrecognised value deserialises to.** The permissive reading
  ("written before authority existed, keep the older guarantee") was rejected: an absent field is
  indistinguishable from a stripped one and from a record produced under the rules being replaced.
* `PROCESS_OBSERVATION` — the engine's own observations *about* the process. True, and not evidence
  about tests.
* `TRUSTED_HARNESS` — produced through the engine's invocation, shown by the differential control to
  depend on the implementation, in a workspace declared non-adversarial. **Not "authenticated".**
* `INDEPENDENT_VALIDATION` — established outside this runtime. Nothing in this runtime produces it.

`BEHAVIOURAL_FLOOR = TRUSTED_HARNESS`. The level is enforced in `verify_code`, `verify_criterion`,
`verify_task`, `_judgment_grounding`, `mission_completion_check` (via the criterion receipts), the
reality anchor's blind assessment, the progress metric, and it round-trips through persistence.

## 4. Every reproduced exploit and its outcome

All attack rows below were re-run **with the workspace declared non-adversarial**
(`trust_workspace_code=True`) — the configuration in which a forgery has the best chance — so that
each is blocked by a real control rather than by the blanket policy.

| attack | evidence | authority | criterion | gate | ground truth |
| --- | --- | --- | --- | --- | --- |
| workspace `pytest.py` shadow | failed | process_observation | False | failed | 999.0 |
| `conftest.py` writes the report, `os._exit(0)` | inconclusive | untrusted_self_report | False | failed | 999.0 |
| `conftest.py` hookwrapper flips failed→passed (**genuine** report) | inconclusive | untrusted_self_report | False | failed | 999.0 |
| `conftest.py` unregisters the junitxml plugin, writes it itself | inconclusive | untrusted_self_report | False | failed | 999.0 |
| subject monkeypatches `_pytest.junitxml.LogXML` | inconclusive | untrusted_self_report | False | failed | 999.0 |
| hostile test module, no conftest/plugin/config at all | inconclusive | untrusted_self_report | False | failed | 999.0 |
| replay of a real report from a correct run | inconclusive | untrusted_self_report | False | failed | 999.0 |
| report describing a different suite | inconclusive | untrusted_self_report | False | failed | 999.0 |
| internally inconsistent report (header says 9, one testcase) | inconclusive | untrusted_self_report | False | failed | 999.0 |
| `pytest.ini` `addopts = -p forge` | inconclusive | untrusted_self_report | False | failed | 999.0 |
| ambient `PYTEST_ADDOPTS=-p forge` | inconclusive | untrusted_self_report | False | failed | 999.0 |
| workspace `sitecustomize.py` | inconclusive | untrusted_self_report | False | failed | 999.0 |
| `unittest` with a forged stdout summary | inconclusive | untrusted_self_report | False | failed | 999.0 |
| **CONTROL** honest workspace, correct implementation | passed | trusted_harness | True | **passed** | 110.0 |
| **CONTROL** honest workspace, wrong implementation | failed (3 real failures) | process_observation | False | failed | 999.0 |
| **CONTROL** the same honest workspace under the **production default** | inconclusive | process_observation | False | failed | 110.0 |

`--noconftest` is **not** claimed as a control, and there is a test that says why: with conftest
loading disabled, an attack moved into the test module still produced a passing report from a
failing suite.

### Process authorization

Each of these was an authorization **bypass**, not a discovery gap — the literal form of the same
command is denied, and these were allowed and the effect landed on disk. Every regression asserts the
effect (the file, the directory, the secret), not a classifier label.

| attack | before | after |
| --- | --- | --- |
| `OUT=<outside>/x; echo pwned > $OUT` | allowed, file written | denied, no file |
| `cd <outside> && echo pwned > x.txt` | allowed, file written | denied, no file |
| `echo pwned > \`echo <outside>/x\`` and `$(…)` | allowed, file written | denied, no file |
| `python3 -c "open('<outside>/x','w').write(…)"` | allowed, file written | denied, no file |
| `X="rm -rf"; $X doomed` | allowed, **directory deleted** | denied, directory intact |
| `V=env; cat .$V` and `cat .e*` | allowed, **secret returned** | denied, secret not in output |
| `git -c alias.pwn='!…' pwn` | allowed, program executed | denied, no marker file |
| `allow_shell=False` + `git` | git still spawned processes | denied |
| writes outside the roots via `run_tests` | allowed | denied |
| **CONTROL** `echo legit > inside.txt`, `ls`, `awk '{print $1}' f` | allowed | still allowed |

The rule is `firewall.unanalysable_command`: variable expansion, command substitution, an inline
interpreter (`eval`, `sh -c`, `python -c`, `perl -e`, `node -e`, `xargs`), or an in-command `cd`
means the effects are not in the command text, so the command is refused rather than guessed at.
Single-quoted spans are excluded first, so `awk '{print $1}'` is unaffected. `git_indirection` covers
`-c`, aliases, `core.pager`/`sshCommand`/`hooksPath`, credential helpers, filters and pack programs.

**Static inspection of command text is not, and cannot be made into, an authorization boundary.**
This narrows the mechanism instead of overstating the parser; real containment belongs in the worker
environment.

## 5. Other weaknesses resolved

**`unittest` / `nose`.** Reachable and *inverted*: the branch accepted forgeries (one
`print("3 passed in 0.05s")` in an imported module) and rejected genuine runs (stdlib unittest's
summary does not match a pytest-shaped regex). There is no trusted path for either, so
`report_required` now covers every recognised framework: their runs are diagnosis and close nothing.
Nothing falls back from the stronger verifier to the weaker evidence.

**Input binding.** `MAX_BOUND_WORKSPACE_FILES` returned an empty list both for "nothing to bind" and
"too many to bind", and the caller could not tell them apart — this repository has ~6200 eligible
files, so every verification rooted at it bound nothing while looking bound. An oversized tree now
produces an authoritative `input_binding` INCONCLUSIVE. An implementation outside the directory under
test is covered separately: it is not bound *and* not withheld by the control, so the pass is not
attributable and a post-verification swap blocks completion.

**Artifacts.** `verify_artifact` establishes that a path resolved to a regular non-empty file whose
bytes hash to H — integrity, never content. A 20-byte `TODO: write this up` produced an outcome
byte-for-byte identical to a finished report at every stage. Now: `verified_scope` records
`existence` or `existence+content`, and only `existence+content` — a declared `ArtifactExpectation`
(`min_bytes`, `must_contain`, `must_not_contain`, `must_match`) checked by the engine — can satisfy a
criterion, ground a judgement, count as task success, count as progress, or support the anchor's
second opinion. The expectation must be declared in advance; one invented after reading the file is
satisfied by whatever the file happens to say. An unrelated artifact is additionally excluded by a
relation test (meeting notes no longer ground a GDPR-compliance judgement).

**Gate applicability.** The aggregator now distinguishes four states, and a SKIPPED check counts as
INAPPLICABLE only if it declares the rule that excused it; an undeclared skip is an unanswered
question and blocks. INCONCLUSIVE blocks.

| check | applicability rule |
| --- | --- |
| `success_criteria` | always applicable |
| `tests` | always applicable |
| `contradictions`, `blocked_operations`, `human_requests` | always applicable |
| `artifact_integrity` | inapplicable only when no artifact is presented as verified **and** no criterion was closed by an artifact receipt; the combination of "a criterion rests on an artifact receipt" with "no verified artifact" is a FAILED contradiction, not a skip |
| `required_artifacts` | inapplicable only when the **compiled mission** declares no deliverable — now derived deterministically by `derive_required_artifacts` from the specification and the criteria, instead of from an optional model-authored field that compilation never filled |
| `receipt_input_versions` | always applicable |
| `input_binding` | raised by `verify_code` when the tree could not be bound |

## 6. Validation

| check | scope | result |
| --- | --- | --- |
| `tests/cogos` | 612 tests (562 before + 50 new trust-boundary regressions) | **612 passed, 0 failed** |
| `make cogos-eval` | 20 scenarios, 12 metrics | **20/20**, all metrics at target |
| `make cogos-demo` | end-to-end scripted mission | reaches `complete` |
| `ruff` | `cogos`, `tests/cogos` | clean |
| `ty` | `cogos` | clean |
| mutation audit | `trust-boundary-mutation-audit.sh`, 25 guards | **25 load-bearing, 0 problems** |
| forbidden-shortcut audit | diff vs `25ec140` | 0 skip/xfail added, 0 tests deleted, 12 lines removed (6 are fixture bodies, 6 are mechanisms policy now refuses — each replaced by a stronger assertion) |
| repository-wide collection | **reported separately** | 39458/39472 collected, **21 collection errors** in `tests/gateway/` and `tests/integration/` from `ModuleNotFoundError: acp, aiohttp` — pre-existing, unrelated to this change, and **repository-wide execution has never been run** |

The mutation audit requires each mutation to change the source checksum (a no-op is reported as a
harness defect) and requires the regression named for each guard to fail. Seven guards initially
reported STILL PASSES — each was shadowed by another layer, so the tests were changed to isolate
their own control rather than the mutations being softened. **Mutation coverage is not proof that
untested attacks are impossible.**

## 7. Files changed

`cogos/verification/attestation.py` (new), `cogos/verification/engine.py`,
`cogos/verification/test_outcome.py`, `cogos/verification/reality_anchor.py`,
`cogos/governance/firewall.py`, `cogos/tools/fabric.py`, `cogos/config.py`,
`cogos/mission/compiler.py`, `cogos/executive/loop.py`, `cogos/executive/progress.py`,
`cogos/executive/anchor_service.py`, `cogos/schemas/mission.py`, `cogos/schemas/verification.py`,
`cogos/schemas/anchor.py`, `cogos/evaluation/demo.py`, `cogos/evaluation/support.py`,
`tests/cogos/test_trust_boundary.py` (new, 50 tests),
`docs/cogos/evidence/trust-boundary-mutation-audit.sh` (new), and six existing test modules.

## 8. Migration behaviour for legacy records

Persisted state loads unchanged — every new field has a default, and the persistence suite covers a
real pre-repair snapshot. What changes is meaning, not loadability: a `TestRecord` or
`VerificationResult` with no `authority` deserialises to `UNTRUSTED_SELF_REPORT` and cannot close a
behavioural criterion. A mission resumed from a pre-repair snapshot therefore has to re-run its
verification. That is the intended cost: the permissive alternative would honour exactly the records
the pre-repair rules admitted. Historical evidence is retained and never deleted.

## 9. Corrections to earlier statements

1. **JUnit report authenticity.** `INCIDENT_REPAIR.md` and the `junit_counts` docstring claimed a
   report at an engine-chosen path could not come from a runner that never ran. **Withdrawn as
   false.** An engine-selected path does not authenticate the producer.
2. **A regression the previous repair introduced.** `report_backed` suppressed the multi-summary
   ambiguity guard, so fabricating a report was strictly *better* for an attacker than not
   fabricating one. Fixed; the guard is now unconditional and has its own regression.
3. **Skipped remote checks.** The previous report described all 14 remote checks as skipped due to
   fork gating. **Skipped CI is not executed validation**, and no claim in this repository rests on
   it.
4. **COGOS suite versus repository-wide validation.** The 562 (now 612) figure is `tests/cogos`
   only. Repository-wide, collection has 21 pre-existing errors and **execution has never been
   run**. The two must not be merged.
5. **"Repair complete."** It was not. Twelve complete false-completion paths survived it.
6. **Live Run #3 readiness.** The previous document treated the run as ready pending a pre-run gate.
   It was not ready, and it is not ready now.
7. **One overclaim inside this repair, corrected during it.** An intermediate note said the trusted
   harness "neutralises the config, plugin, env and import-path routes" without stating that the
   harness is not a boundary. It closes those routes at the source; it does not stop in-process
   manipulation, and the module docstring now says so.

## 10. Classification: BLOCKED

**BLOCKED.** Every reproduced complete false-completion path is now blocked, and the honest controls
still work — but the primary control against an adversarial workspace (the differential control) is
a sensitivity check rather than an enforceable boundary, and the runtime's own default says so by
refusing in-process evidence outright.

**The smallest missing primitive:** *an execution boundary the subject process cannot cross* — the
test runner running as a different uid, with the workspace mounted read-only except for a declared
output path, and an evidence channel (a pipe or a file) the subject cannot write. With that, a report
is attributable to the harness rather than corroborated by a control, `TRUSTED_HARNESS` means what
its name says, and `trust_workspace_code` stops being a human judgement call.

### Remaining weaknesses

* **Adaptive adversary.** Code that detects the withheld-implementation control and answers the two
  runs differently defeats it. Unmitigated, by design, and documented rather than hidden.
* **Report replacement race.** The report is read twice and refused if the bytes move, but a process
  that outlives the runner and rewrites between the two reads is not excluded.
* **Static command inspection.** Narrowed, never sound. A command the parser cannot resolve is now
  refused, which is a restriction, not a boundary.
* **Unattestable-but-honest work.** A suite with no non-test module under the directory under test
  cannot be attested and will not close a criterion. Fail-closed and intended; it is a real false
  negative.
* **Content expectations are only as good as what the mission declares.** The engine checks them
  deterministically; it does not know whether they are the right ones.
* **`INDEPENDENT_VALIDATION` is unreachable** from inside the runtime, by construction.

### If a live run is ever justified

The protocol in [`LIVE_RUN_3_PROTOCOL.md`](LIVE_RUN_3_PROTOCOL.md) has been updated with the
restrictions this classification implies. **It has not been executed, and must not be while the
classification is BLOCKED.**
