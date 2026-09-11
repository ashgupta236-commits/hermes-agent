# Isolated behavioural verification — implementation and outcome

**Live Run #3 was not executed.** The readiness gate is BLOCKED on an external condition recorded in
[`READINESS.md`](READINESS.md). Everything below is offline evidence.

| | |
| --- | --- |
| Starting SHA | `5c960714a153dbd9980593bc6f8d4debeebaf7ab` (clean tree) |
| Branch | `claude/autonomous-cognitive-os-og6r0p` |
| Backend | Docker 29.3.1 / runc / cgroup v1 / seccomp, pinned image, **no gVisor** |
| Decision record | [`ISOLATION_DECISION.md`](ISOLATION_DECISION.md), written before implementation |

---

## 1. What changed, and why it is different from the previous rounds

Every previous round read something the subject's process wrote and tried to decide whether to
believe it. The honest conclusion from that work was that no property of a self-report distinguishes
an honest one — a random filename, a nonce, a hash, a trusted launcher and the exit code were each
defeated, and a `conftest.py` hookwrapper defeated all of them at once by making the *genuine* runner
write a genuine report whose contents were false.

This round changes the question. The controller chooses the inputs, holds the expected answers, and
compares. The subject never receives an expectation, so there is nothing for it to agree with.

```
trusted controller + verifier (holds the expected answers)
  → bounded JSON request
    → isolated subject process (uid 65534, no caps, no network, read-only source, seccomp)
  → untrusted JSON response
→ trusted comparison against the approved contract
→ controller-issued receipt (snapshot + contract + policy + image digests)
→ criterion → completion gate
```

The verifier never imports, executes, evaluates or deserializes anything from the workspace. It
hashes bytes, parses JSON scalars, and runs one `ast.parse` in a resource-bounded trusted subprocess.

### New components

| file | what it is |
| --- | --- |
| `cogos/verification/isolation.py` | the execution boundary: one backend, a policy with a **floor**, streaming bounded I/O, unconditional teardown |
| `cogos/verification/protocol.py` | the bounded data protocol and the in-sandbox adapter; strict JSON, fixed ops, request identity, finite numbers, size caps |
| `cogos/verification/contract.py` | the approved acceptance contract derived from `REQUIREMENTS.md`, digest-bound, plus the criterion→predicate mapping |
| `cogos/verification/behavioural.py` | the trusted verifier: snapshot, isolate, compare, decide |
| `cogos/verification/runtime/` | the pinned runtime image and a portable build script |
| `tests/cogos/test_isolated_verifier.py` | 68 acceptance tests |
| `docs/cogos/evidence/isolated-verifier-mutation-audit.sh` | 42 mutation guards |
| `docs/cogos/evidence/readiness-check.py` | the readiness gate, machine-produced |

## 2. Backend selection — measured, not assumed

gVisor (`runsc`) is **absent** on this host, as are `podman`, `crun` and `bwrap`. Docker 29.3.1 with
`runc` is present; the daemon was not running at session start and starts cleanly. cgroup v1
controllers are mounted and cgroup2 is mountable.

**Chosen: Docker + runc + cgroup v1 + seccomp with a pinned image, under an explicit host-kernel
trust assumption.** An ordinary subprocess is never substituted: `run_isolated` raises
`IsolationUnavailable` and the caller refuses.

Every restriction was verified by attempting the thing and observing the outcome:

| restriction | probe result |
| --- | --- |
| read-only source | write **refused**; the on-disk canary unchanged (world-writable on purpose, so the mount flag is what refuses, not file ownership) |
| bounded scratch | tmpfs `size=16m`; a 64 MiB write stops at exactly 16 MiB |
| read-only rootfs | `/tmp/escape` refused (mode 1777 in the image, so only the flag can refuse it) |
| `/dev/shm` bounded | 64 MiB attempt lands ≤ 16 MiB |
| no controller state | `/home/user`, `/var/run/docker.sock`, `/root/.ccr/...` all absent; no controller credentials in the subject's environment |
| own PID namespace | 3 visible PIDs; `/proc/1` is the subject's own interpreter |
| non-root, no caps, no escalation | uid **65534**, `CapEff: 0000000000000000`, `NoNewPrivs: 1` — and capabilities stay dropped even for a deliberately root subject |
| syscall restriction | `Seccomp: 2` (filter) |
| network disabled | outbound TCP, DNS **and** `169.254.169.254` all refused |
| memory / processes | 512 MiB under `--memory 256m` → exit **137**; fork refused at 63/64 |
| output | streamed and capped; a flood is discarded past the cap rather than buffered |
| teardown | every exit path kills and removes the container; no host survivors |

## 3. The acceptance contract

Derived from the bytes of `REQUIREMENTS.md`, digest-bound, and decided before any deliverable
exists. Each requirement maps to **one** predicate with a stated scope.

| requirement | predicate | scope of the claim |
| --- | --- | --- |
| `calc.py` exists | present in the hashed snapshot | existence |
| `add_percent` raises a value by a percent, 2dp | 18 behavioural cases compared on the trusted side | the observed behaviour **on the cases tested** |
| `test_calc.py` exists | present in the hashed snapshot | existence |
| tests cover positive, zero, negative | trusted AST structure of the suite's sources | that the sources have that shape |
| (inferred) the suite passes | the suite run in the boundary against the deliverable **and** against a controller-substituted wrong implementation | that the suite distinguishes those two |

Seven cases come from the requirement's own wording and boundaries; eleven are drawn from a
controller-held seed the subject never sees, so an implementation that special-cases the published
examples does not pass. Tie cases at a `.xx5` boundary are excluded, because the requirement does not
say how ties break and a contract must not invent an expectation.

The contract's own `limitations` are part of its digest. Leaving them out meant a contract claiming
"establishes universal correctness" hashed identically to this one.

## 4. The vertical slice, at production defaults

`governance.trust_workspace_code` is `False` and is asserted `False` inside every fixture.

| case | criteria | gate | authority | ground truth |
| --- | --- | --- | --- | --- |
| correct implementation | `[True, True]` | **passed** | `trusted_harness` | 110.0 |
| `return 999.0` | `[False, False]` | blocked | `process_observation` | 999.0 |
| forged protocol + `authority` + exit 0 | `[False, False]` | blocked | `process_observation` | 999.0 |
| deliverable swapped after verification | — | blocked on artifact integrity | — | 999.0 |

This is the first configuration in this line of work in which correct work completes **without a
trust exemption**.

## 5. Adversarial review of this subsystem, and what it found

Four independent reviewers attacked the new code — protocol, isolation, contract, integration — with
their own reproductions. **They found real defects in code written the same day**, including two
fatal. All are fixed, and each has a regression and a mutation guard.

| finding | what it was | fix |
| --- | --- | --- |
| **stdin hijack (fatal)** | the deliverable's *module-level* code runs before the adapter's loop, so `calc.py` could read the requests itself, compute correct answers from the requirement, and ship a wrong `add_percent`. Satisfied=True, `trusted_harness`, wrong on 13 of 18 cases | the verifier withholds stdin until the adapter reports the import finished; such a subject now blocks on an empty pipe and the run times out |
| **unbounded controller buffering (fatal)** | `capture_output=True` materialised the subject's whole stdout in the controller — 926 MiB measured, RSS 1.9 GiB — before the 512 KiB cap applied | streamed reads with a hard cap; excess discarded, never stored |
| **container leak on any non-timeout exit** | only the timeout path tore the container down; `--rm` fires when the container stops, and nothing was stopping it | teardown in a `finally`, on every path |
| **no floor on the policy** | the restrictions were only *default field values*; `IsolationPolicy(network="host", read_only_rootfs=False, ...)` produced a receipt indistinguishable from a confined run | `validate()` refuses any policy below the boundary, called before every run |
| `UnicodeDecodeError` from one byte | `text=True` decodes strictly; the subject chooses the bytes | decode with replacement |
| `OverflowError` from a 400-digit integer | `OverflowError` is an `ArithmeticError`, so `except ValueError` missed it | caught; the value is refused |
| unpaired surrogate poisons state | seven ASCII characters made the mission unserialisable | subject text sanitised before it reaches durable state |
| suite differential fail-open | any non-zero mutant exit counted as discrimination, **including exit 125** — a docker hiccup turned a vacuous suite into a passing check | 125 and any recorded backend failure are "nothing established", not discrimination |
| `IsolatedRun.failure` subject-authored | filled from the subject's own stderr, in a field documented as the controller's observation | the controller's observation of the exit code alone, no subject text |
| symlinked deliverables | `is_file()` and `copyfile` follow links; a link to a procfs file reports `st_size` 0 and reads back thousands of bytes, walking through the size cap | symlinked deliverables refused; the copy is bounded by bytes actually read |
| `/dev/shm` unbounded | a second writable tmpfs the policy never named, sized by daemon default | `--shm-size` is a policy field and therefore in the digest |
| parametrized suites rejected | the canonical `@pytest.mark.parametrize` scored 1 test and 0 percent literals — a **false negative against correct work** | parametrized rows count as the cases they are, and the `percent` column is read |
| suite saw only the deliverables | a correct suite using a `conftest.py` fixture failed both runs, and the message blamed the suite | the suite runs see the whole workspace |
| contract digest omitted limitations | see §3 | included |

Two reviewer findings are **not fixed, by design**, and are recorded as limitations in §7: a
deliverable that detects the boundary and behaves correctly only inside it, and a suite whose
discriminator is non-behavioural.

**One correction about the review itself.** Several findings were marked "refuted" by the verification
pass. That is a race, not a refutation: they were real when filed and I had already fixed them while
the reviewers were verifying. They are listed above as found, because that is what happened.

## 6. Validation

| check | scope | result |
| --- | --- | --- |
| acceptance matrix | `tests/cogos/test_isolated_verifier.py`, real backend, **0 skipped** | **68 passed** |
| full COGOS suite | `tests/cogos` | **682 passed, 0 failed** |
| `ruff` | `cogos`, `tests/cogos` | clean |
| `ty` | `cogos` | clean |
| mutation audits | three scripts, **84 guards** | incident-repair 17/17, trust-boundary 25/25, isolated-verifier 42/42, **0 problems** |

Every mutation must change the source checksum, and each guard's named regression must fail when it
is reverted. Eleven guards initially reported `STILL PASSES` and three `MUTATION DID NOT APPLY`
across the three audits; in every case the **test or the harness** was corrected so the guard is
isolated — no mutation was softened.

**A correction to the previous round.** Four of the incident-repair audit's guards had been broken
since `5c96071` — the trust-boundary repair rewrote lines that audit targets, and I only ran the
other audit. The previous report's claim that both audits were clean was wrong. Both are clean now,
and the incident audit was hardened to exit non-zero so a future breakage cannot pass unnoticed.

Repository-wide collection is reported separately and is unchanged: 21 pre-existing collection errors
in `tests/gateway/` and `tests/integration/` from `ModuleNotFoundError: acp, aiohttp`.
**Repository-wide execution has never been run.**

## 7. What this establishes, and what it does not

**Establishes.** That the subject executed under the stated restrictions; that its observed responses
to controller-chosen inputs did or did not match controller-held expectations; the byte identity of
the snapshot those responses came from; and that a criterion mapped to the approved contract cannot
be closed any other way.

**Does not establish, and is not claimed:**

* **Universal correctness.** Finite cases establish conformance on the cases tested. An
  implementation that special-cases exactly them passes. The seed raises the cost; it does not close
  it.
* **Immunity to a boundary-detecting deliverable.** The subject runs in exactly one place and that
  place is identifiable from inside. An implementation correct only inside the container is judged
  satisfied. Closing this needs the deliverable exercised somewhere the subject cannot recognise,
  which is a different piece of work.
* **That the suite is good.** `suite_differential` establishes that the suite distinguishes the
  deliverable from *one controller-chosen* replacement. A non-behavioural discriminator — a module
  attribute, a docstring — produces that difference too. It is a floor, not a measure of coverage.
* **That structural coverage is coverage.** The AST predicate says percent literals of each sign
  appear in the suite's sources. When they are held in a fixture or data file it reports
  *undeterminable* and blocks — an absence of evidence, deliberately not reported as the suite
  omitting the cases.
* **Immunity to host-kernel compromise.** runc shares the host kernel. gVisor is absent here and
  would reduce, not remove, that exposure.
* **`INDEPENDENT_VALIDATION`.** Still unreachable from inside this runtime, by construction.

### Also recorded, not fixed

* `map_criteria` is bag-of-words matching against a different bag, and it hands out *this* contract's
  predicates whatever the criterion says. It is correct for this single-deliverable mission and must
  not be reused for a multi-feature mission without extending the contract.
* `residency_ok` is `True` when residency is `UNKNOWN` (no `modelUsage` returned). On a failed call
  this is moot — the call already failed — but "unknown" is not "verified", and the protocol requires
  recording both fields for that reason.
* `probe_backend()` records `security_options`, `runtime` and `gvisor` into provenance but nothing
  compares them against a requirement, so two hosts with different confinement produce identical
  policy digests.
