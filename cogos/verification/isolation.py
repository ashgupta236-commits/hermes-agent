"""The execution boundary: one backend, named restrictions, verified by effect.

The subject runs in a container it cannot see out of. What makes this a boundary rather than a
subprocess is that every restriction below was measured by attempting the thing and observing the
refusal — see ``docs/cogos/evidence/ISOLATION_DECISION.md`` §4:

* the source snapshot is mounted read-only and a write to it is refused;
* the container rootfs is read-only; the only writable place is a size-capped tmpfs;
* no controller home, repository, evidence store, credential path or docker socket is mounted;
* no host PID or network namespace; networking is off, including instance metadata;
* the subject runs as uid 65534 with ``CapEff: 0000000000000000``, ``NoNewPrivs: 1`` and an active
  seccomp filter;
* memory, CPU, process count, output size and wall clock are all bounded, and a container that
  exceeds memory is killed by the cgroup rather than asked to stop;
* killing the container reaps its descendants, with no survivors on the host.

**gVisor is not installed on this host**, so the boundary is runc's: it shares the host kernel and a
kernel or runc vulnerability defeats it. That assumption is documented rather than hidden, and it is
the reason this module refuses instead of degrading when the backend is unavailable — an ordinary
subprocess is not a substitute.

The policy is constructed by the controller. The executive cannot name an image, a mount, a
capability, a security option or a limit; there is no parameter on any tool that reaches this.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional, Sequence

#: The runtime the subject executes in, built from `cogos/verification/runtime/Dockerfile` with every
#: version pinned. It is named by tag here, and the *resolved image ID* — a content digest of the
#: built filesystem — is recorded in provenance on every run, so a receipt still names one exact
#: filesystem even though the policy names a tag.
DEFAULT_IMAGE = "cogos-verifier:1"
#: The base the runtime is built from, pinned by registry digest.
BASE_IMAGE = "python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534"
BUILD_COMMAND = "docker build -t cogos-verifier:1 cogos/verification/runtime"

#: Where the snapshot and the engine-authored adapter appear inside the container. They are separate
#: mounts so that a workspace file cannot shadow the adapter.
SUBJECT_MOUNT = "/subject"
HARNESS_MOUNT = "/harness"
SCRATCH_MOUNT = "/scratch"


class IsolationUnavailable(RuntimeError):
    """The boundary could not be established. Never degraded into an unisolated run."""


@dataclass(frozen=True)
class IsolationPolicy:
    """Everything that constrains the subject, in one hashable object."""

    image: str = DEFAULT_IMAGE
    user: str = "65534:65534"
    network: str = "none"
    memory: str = "256m"
    cpus: str = "1.0"
    pids_limit: int = 64
    scratch_size: str = "16m"
    #: `/dev/shm` exists and is writable whatever the policy says — the daemon creates it. Sizing it
    #: here makes it part of the policy and therefore part of the digest, instead of a writable
    #: surface whose bound moves with daemon configuration while the receipt stays identical.
    shm_size: str = "16m"
    read_only_rootfs: bool = True
    drop_all_capabilities: bool = True
    no_new_privileges: bool = True
    wall_clock_seconds: int = 60
    #: How long the adapter has to import the deliverable and announce itself. Bounded separately
    #: from the whole run because a subject that reads stdin at import blocks forever, and waiting
    #: the full wall clock for that is a minute of nothing. Importing one module is fast; the
    #: request loop gets the remaining budget.
    handshake_seconds: int = 20
    max_output_bytes: int = 512 * 1024

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode("utf-8")).hexdigest()

    def validate(self) -> None:
        """Refuse a policy that has been relaxed below the boundary this module claims.

        The restrictions in the docstring were only *default field values*: nothing stopped a caller
        constructing `IsolationPolicy(network="host", read_only_rootfs=False, drop_all_capabilities=
        False)` and getting a receipt that looks identical to a confined one. A policy digest that
        can name an unconfined run is not provenance. `user` is deliberately not on this list — a
        root-subject probe is a legitimate diagnostic and is still capability-dropped — but
        everything that would actually open the boundary is.
        """
        problems = []
        if self.network != "none":
            problems.append(f"network={self.network!r}: this verification runs with no network")
        if not self.read_only_rootfs:
            problems.append("read_only_rootfs is off")
        if not self.drop_all_capabilities:
            problems.append("capabilities are not dropped")
        if not self.no_new_privileges:
            problems.append("no-new-privileges is off")
        if self.pids_limit <= 0:
            problems.append(f"pids_limit={self.pids_limit}: must be a positive bound")
        if not self.memory or self.memory in ("0", "0b"):
            problems.append("memory is unbounded")
        if not self.shm_size or self.shm_size in ("0", "0b"):
            problems.append("/dev/shm is unbounded")
        if self.handshake_seconds <= 0:
            problems.append("the handshake bound must be positive")
        if self.wall_clock_seconds <= 0 or self.max_output_bytes <= 0:
            problems.append("wall clock and output bounds must be positive")
        if problems:
            raise IsolationUnavailable("the isolation policy is below the required floor: " + "; ".join(problems))

    def docker_args(self, *, name: str, snapshot: Path, harness: Path) -> list[str]:
        args = [
            "docker", "run", "--rm", "--name", name,
            "--network", self.network,
            "--user", self.user,
            "--pids-limit", str(self.pids_limit),
            "--memory", self.memory, "--memory-swap", self.memory,
            "--cpus", self.cpus,
            "-v", f"{snapshot}:{SUBJECT_MOUNT}:ro",
            "-v", f"{harness}:{HARNESS_MOUNT}:ro",
            "--tmpfs", f"{SCRATCH_MOUNT}:rw,size={self.scratch_size},mode=1777,noexec,nosuid,nodev",
            "--shm-size", self.shm_size,
            "-w", SCRATCH_MOUNT,
            # A read-only rootfs leaves no writable temp directory, and a test runner needs one:
            # without TMPDIR pointing at the scratch tmpfs, pytest dies in `_get_default_tempdir`
            # before collecting anything, and the verifier would read that as the suite failing.
            "-e", "PYTHONSAFEPATH=1", "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", "HOME=" + SCRATCH_MOUNT, "-e", "TMPDIR=" + SCRATCH_MOUNT,
            "-i",
        ]
        if self.read_only_rootfs:
            args.append("--read-only")
        if self.drop_all_capabilities:
            args += ["--cap-drop", "ALL"]
        if self.no_new_privileges:
            args += ["--security-opt", "no-new-privileges"]
        args.append(self.image)
        return args


@dataclass
class IsolatedRun:
    """What the controller observed about the execution. Not a statement about behaviour."""

    started: bool
    started_at: float
    finished_at: float
    exit_code: Optional[int]
    timed_out: bool
    stdout: str
    stderr: str
    truncated: bool
    container_name: str
    image_id: str = ""
    failure: str = ""

    @property
    def wall_seconds(self) -> float:
        return round(self.finished_at - self.started_at, 3)

    def facts(self) -> dict[str, object]:
        return {
            "started": self.started,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "wall_seconds": self.wall_seconds,
            "stdout_bytes": len(self.stdout.encode("utf-8", "replace")),
            "truncated": self.truncated,
            "container": self.container_name,
            "image_id": self.image_id,
            "failure": self.failure,
        }


@dataclass
class BackendReport:
    """Whether the boundary is actually available, and what it is."""

    available: bool
    backend: str = ""
    server_version: str = ""
    runtime: str = ""
    cgroup_version: str = ""
    security_options: tuple[str, ...] = ()
    gvisor: bool = False
    missing: str = ""

    def as_dict(self) -> dict[str, object]:
        return {**asdict(self), "security_options": list(self.security_options)}


def _docker(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)


def probe_backend() -> BackendReport:
    """Ask the backend what it is, rather than asking whether a binary exists."""
    if shutil.which("docker") is None:
        return BackendReport(available=False, missing="the docker client is not installed")
    try:
        info = _docker("info", "--format", "{{json .}}", timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return BackendReport(available=False, missing=f"docker info failed: {exc}")
    if info.returncode != 0:
        return BackendReport(available=False, missing=f"the docker daemon is not reachable: {info.stderr.strip()[:200]}")
    try:
        data = json.loads(info.stdout)
    except ValueError:
        return BackendReport(available=False, missing="docker info returned unparsable output")
    runtimes = data.get("Runtimes") or {}
    return BackendReport(
        available=True,
        backend="docker",
        server_version=str(data.get("ServerVersion", "")),
        runtime=str(data.get("DefaultRuntime", "")),
        cgroup_version=str(data.get("CgroupVersion", "")),
        security_options=tuple(str(o) for o in (data.get("SecurityOptions") or [])),
        # Recorded because its absence is the documented gap in this boundary, not a detail.
        gvisor=any("runsc" in str(k) for k in runtimes),
    )


def resolve_image_id(image: str) -> str:
    """The content digest of the image that will actually run, not the name it was asked for."""
    inspected = _docker("image", "inspect", image, "--format", "{{.Id}}", timeout=60)
    return inspected.stdout.strip() if inspected.returncode == 0 else ""


def ensure_image(policy: IsolationPolicy) -> str:
    """Make the pinned image present and return its resolved ID, or refuse.

    A locally built runtime is never "pulled from somewhere that happens to have that tag": if it is
    missing, the caller is told the exact command that produces it. Substituting a different image
    silently is how a receipt ends up naming a filesystem that never ran.
    """
    resolved = resolve_image_id(policy.image)
    if resolved:
        return resolved
    if "/" in policy.image or "@" in policy.image:
        pulled = _docker("pull", "--quiet", policy.image, timeout=900)
        if pulled.returncode != 0:
            raise IsolationUnavailable(f"the pinned runtime image is unavailable: {pulled.stderr.strip()[:300]}")
        return resolve_image_id(policy.image)
    raise IsolationUnavailable(
        f"the runtime image {policy.image!r} is not present. Build it with: {BUILD_COMMAND}"
    )


def _drain(stream, sink: bytearray, cap: int, over: list[bool], ready: Optional[bytes], seen) -> None:
    """Read a pipe to EOF, keeping at most `cap` bytes.

    The point is that the reading never stops: a pipe that is not drained blocks the writer, and a
    subject blocked on write is a subject that cannot be killed cleanly. So everything is read and
    only the first `cap` bytes are kept — the alternative, `capture_output=True`, materialises the
    subject's entire output in the controller's address space before any cap is applied, which was
    measured at 926 MiB of controller memory for a subject that only writes to stdout.
    """
    try:
        for chunk in iter(lambda: stream.read(65536), b""):
            if len(sink) < cap:
                sink.extend(chunk[: cap - len(sink)])
            else:
                over[0] = True
            if ready is not None and not seen.is_set() and ready in bytes(sink):
                seen.set()
    except (OSError, ValueError):
        pass
    finally:
        if seen is not None:
            seen.set()  # never leave a handshake waiter blocked on a stream that has ended
        try:
            stream.close()
        except OSError:
            pass


def run_isolated(
    policy: IsolationPolicy,
    *,
    snapshot: Path,
    harness: Path,
    argv: Sequence[str],
    stdin_data: str = "",
    handshake: Optional[str] = None,
    name_hint: str = "cogos",
) -> IsolatedRun:
    """Run `argv` against the snapshot inside the boundary and report what was observed.

    Raises :class:`IsolationUnavailable` when the boundary cannot be established or the policy is
    below its floor. That is a refusal, not a result: the caller must not read it as evidence.

    When `handshake` is given, stdin is withheld until that marker appears on stdout. That ordering
    is a control, not a convenience: module-level code in the subject runs *before* the adapter's
    request loop, and a deliverable that reads stdin at import time can answer the protocol itself.
    Withholding the requests until the adapter says it has finished importing means such a subject
    blocks on an empty pipe and the run times out instead of producing answers.
    """
    policy.validate()
    report = probe_backend()
    if not report.available:
        raise IsolationUnavailable(report.missing)
    image_id = ensure_image(policy)

    name = f"{name_hint}-{os.getpid()}-{int(time.time() * 1000) % 1_000_000}"
    command = policy.docker_args(name=name, snapshot=snapshot.resolve(), harness=harness.resolve()) + list(argv)
    started = time.time()
    deadline = started + policy.wall_clock_seconds
    out_buf, err_buf = bytearray(), bytearray()
    over = [False]
    ready_seen = threading.Event()
    timed_out = False
    exit_code: Optional[int] = None

    try:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    except OSError as exc:
        raise IsolationUnavailable(f"the isolated run could not be launched: {exc}") from exc

    marker = handshake.encode("utf-8") if handshake else None
    readers = [
        threading.Thread(target=_drain, args=(proc.stdout, out_buf, policy.max_output_bytes, over, marker, ready_seen), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, err_buf, policy.max_output_bytes, over, None, threading.Event()), daemon=True),
    ]
    for reader in readers:
        reader.start()

    try:
        if marker is not None:
            # Wait for the adapter to say it has imported the subject, or give up at the deadline.
            handshake_deadline = min(deadline, started + policy.handshake_seconds)
            if not ready_seen.wait(timeout=max(0.0, handshake_deadline - time.time())):
                timed_out = True
        if proc.stdin is not None:
            if not timed_out and stdin_data:
                try:
                    proc.stdin.write(stdin_data.encode("utf-8"))
                except (OSError, ValueError):
                    pass
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass
        if not timed_out:
            try:
                exit_code = proc.wait(timeout=max(0.0, deadline - time.time()))
            except subprocess.TimeoutExpired:
                timed_out = True
    finally:
        # Unconditional. Only the timeout path used to tear the container down, so any other
        # exception — including a MemoryError the subject could provoke — left it running and
        # unsupervised. `--rm` does not help: it fires when the container stops, and nothing was
        # stopping it.
        if proc.poll() is None:
            proc.kill()
        _docker("kill", name, timeout=20)
        _docker("rm", "-f", name, timeout=20)
        for reader in readers:
            reader.join(timeout=10)

    finished = time.time()
    # `errors="replace"`: one invalid UTF-8 byte on the subject's stdout used to raise
    # UnicodeDecodeError out of the controller. The subject chooses these bytes.
    stdout = bytes(out_buf).decode("utf-8", "replace")
    stderr = bytes(err_buf).decode("utf-8", "replace")

    # Exit 125 is docker's "could not run the container" code — and it is also an exit code the
    # subject is free to choose. Previously this field was filled from the subject's own stderr,
    # so a subject could write "docker: Error response from daemon: ..." and have the controller
    # record its own failure as an infrastructure fault. The flag is now the controller's
    # observation of the code alone, with no subject-authored text in it, and callers must treat it
    # as "this may not have been a real run", never as "the backend failed".
    failure = "exit 125: the container may not have started, or the subject chose this code" if exit_code == 125 else ""

    return IsolatedRun(
        started=True,
        started_at=started,
        finished_at=finished,
        exit_code=exit_code,
        timed_out=timed_out,
        stdout=stdout,
        stderr=stderr,
        truncated=over[0],
        container_name=name,
        image_id=image_id,
        failure=failure,
    )
