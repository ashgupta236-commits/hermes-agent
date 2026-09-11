# Decision record — isolated behavioural verification

Written before implementation, against measured capabilities rather than assumed ones. Scope is
the architecture this mission needs and nothing wider.

| | |
| --- | --- |
| Host | `vm`, `/home/user/hermes-agent`, branch `claude/autonomous-cognitive-os-og6r0p` |
| Baseline SHA | `5c960714a153dbd9980593bc6f8d4debeebaf7ab`, clean tree |
| Kernel | 6.18.44-fc-v24 |
| Decision | Docker 29.3.1 / runc / cgroup v1 / seccomp, pinned image digest |

## 1. Attacker

The attacker controls the **mission workspace**: its source, tests, configuration and output. It can
generate arbitrary subject behaviour, forge stdout, manipulate its own process, and attempt
filesystem, process and network abuse. It is the same authority as the executive's task tools — so
"the executive would not do that" is not a control.

### Protected from both the subject and the executive's task tools

| asset | how |
| --- | --- |
| verifier code and expected answers | live only in the controller process and the repository; never mounted into the subject |
| approved mission contract and criterion bindings | controller-owned, digest-bound into every receipt |
| execution policy and authority issuance | `IsolationPolicy` is constructed by the controller; the executive cannot name mounts, images, capabilities or limits |
| evidence storage and receipt creation | only `VerificationEngine` writes receipts; a serialized authority value arriving from outside is data |
| completion-gate implementation | runs in the controller process over controller-owned state |
| controller credentials and runtime-management interfaces | no docker socket, no credential path, no host mount inside the subject |

### Trusted computing base

The controller process, the verifier module and its approved contract, the pinned runtime image,
`dockerd`/`containerd`/`runc`, the host kernel, and the pinned Python dependencies. Everything in
the mission workspace is outside it.

### Residual risks, stated rather than argued away

* **Host-kernel trust.** runc containers share the host kernel. A kernel or runc vulnerability
  defeats this boundary. gVisor (`runsc`) is **not installed here** and would reduce, not remove,
  that exposure.
* **Finite behavioural coverage.** Tests establish conformance to the tested contract on the cases
  tested. They do not prove universal correctness and do not rule out adaptive behaviour — an
  implementation that special-cases exactly the verified inputs passes. Requirement-derived random
  cases raise that cost; they do not eliminate it.
* **The subject-side adapter runs in the subject's process.** Its output is untrusted by
  construction; see §3.
* **Availability, not integrity.** The daemon is started by this session. A run that cannot start
  the backend must refuse, never degrade.

## 2. Corrections to statements in the current documentation

1. **"the engine cannot hash a file it did not author"** — it can. Hashing an attacker-produced
   file establishes **byte identity** of that file, which is exactly what receipt binding needs. It
   establishes nothing about whether the contents are true. The original sentence conflated the two;
   the correct claim is that a hash is an identity primitive, not a truth primitive.
2. **A different UID does not protect a verifier that imports hostile code into its own process.**
   Once hostile code is in the address space, uid is irrelevant. Isolation must prevent the import,
   not relabel the importer.
3. **A separate process is not a boundary by itself.** Without namespaces, read-only mounts,
   dropped capabilities and a restricted syscall surface, a subprocess shares the filesystem and
   can reach the evidence store. "Out of process" ≠ "isolated".
4. **A signed or hashed false report is still false.** Signing binds a report to its producer. When
   the producer is the subject, that authenticates the liar.
5. **Keyword, regex and size predicates establish only themselves.** `must_contain=["findings"]`
   proves the substring is present. It does not prove the document is complete, correct, or a
   report. Any claim beyond the literal predicate is unsupported.

**No new authority levels.** The four existing levels keep their meaning, with one legitimately
reachable for the first time:

* `UNTRUSTED_SELF_REPORT` — anything the subject's process authored: stdout, exit code, JUnit, and
  the protocol response body itself *before* trusted comparison.
* `PROCESS_OBSERVATION` — what the controller observed about the execution: that it started, its
  termination facts, wall time, exit status. True, and not a statement about behaviour.
* `TRUSTED_HARNESS` — **the controller's own comparison** of observed responses against expectations
  it holds, produced under a verified isolation policy. This is the level the isolated behavioural
  verifier issues, and it is what the level was always supposed to mean.
* `INDEPENDENT_VALIDATION` — established outside this runtime entirely. Still not issuable here.

## 3. Architecture

```
trusted controller + verifier (holds expected answers)
  → typed request (bounded JSON, controller-authored)
    → isolated subject process (Docker: no network, read-only source, uid 65534, no caps, seccomp)
  → untrusted response (bounded JSON)
→ trusted comparison against approved expectations
→ controller-issued receipt (snapshot + contract + policy + image digests)
→ criterion → completion gate
```

**The trusted verifier never imports, executes, evaluates or deserializes executable objects from
the subject workspace.** It reads bytes, hashes them, and compares JSON scalars.

A small engine-authored adapter inside the sandbox imports the deliverable and invokes it. That
adapter runs in the subject's process, so **its output is untrusted and its presence authenticates
nothing**. The verifier's authority comes only from comparing observed responses against
expectations the subject never sees.

The subject may emit `"passed": true`, forged JUnit, authority fields or receipt-shaped JSON. None
of it is read as a verdict: the trusted side reads exactly `request_id`, `ok`, `result`, `error`,
and ignores every other key.

## 4. Backend selection — measured, not assumed

Preflight on this host. Probes assert **effects**, not the presence of a binary or a flag.

| capability | result |
| --- | --- |
| gVisor `runsc` | **absent** — the preferred backend is unavailable |
| `podman`, `crun`, `bwrap`, `firejail` | absent |
| `docker` / `dockerd` / `containerd` / `runc` | present, 29.3.1; daemon not running at session start, starts cleanly |
| cgroup | v1 controllers mounted (`cpu`, `memory`, `pids`, `cpuset`, …); cgroup2 also mountable |
| namespaces | user, mount, pid, net, uts, ipc, cgroup all succeed |
| seccomp | filter mode active inside a container (`Seccomp: 2`) |
| registry | reachable; image pinnable by digest |

**Chosen: Docker + runc + cgroup v1 + seccomp, image pinned by digest**, under an explicit
host-kernel trust assumption. An ordinary subprocess is **not** substituted when this is
unavailable: a run that cannot establish the boundary refuses and the mission stays blocked.

### Verified effective restrictions

Every row was measured by attempting the thing and observing the outcome.

| restriction | probe result |
| --- | --- |
| immutable source, read-only | write **refused**; on-disk canary unchanged |
| bounded writable scratch | tmpfs `size=16m`; a 64 MiB write stops at exactly 16 MiB |
| read-only container rootfs | `/etc/passwd` write refused |
| no controller home / repo / evidence / credentials | `/home/user` absent inside; `/root` unreadable |
| no docker socket or management endpoint | `/var/run/docker.sock` **absent** |
| no host PID namespace | 3 visible PIDs |
| non-root subject | uid **65534** |
| capabilities dropped | `CapEff: 0000000000000000` |
| no privilege escalation | `NoNewPrivs: 1` |
| syscall restriction | `Seccomp: 2` (filter) |
| network disabled | outbound TCP, DNS **and** `169.254.169.254` all refused |
| memory limit | 512 MiB allocation under `--memory 256m` → exit **137** (cgroup OOM kill) |
| process limit | `--pids-limit 64` → fork refused at 63 |
| descendant teardown | container killed → exit 137, **no host survivors** |

## 5. What this does and does not establish

**Establishes.** That the subject executed under the stated restrictions; that its observed
responses to controller-chosen inputs did or did not match controller-held expectations; the byte
identity of the source snapshot those responses came from.

**Does not establish.** Universal correctness of the deliverable; absence of adaptive behaviour
outside the tested inputs; immunity to host-kernel compromise; anything at all about content the
contract does not have a predicate for.
