"""The trusted behavioural verifier: the controller decides, the subject only answers.

This is the piece the previous repairs did not have. Every earlier control read something the
subject's process wrote and tried to decide whether to believe it — and the honest conclusion was
that no property of a self-report distinguishes an honest one. Here the question changes: the
controller sends inputs it chose, receives numbers, and compares them against answers it already
holds. The subject never sees an expected value, so there is nothing for it to agree with.

**This module never imports, executes, evaluates or deserializes anything from the subject
workspace.** It reads bytes and hashes them, it parses JSON scalars, and it parses one file with
`ast` in a resource-bounded trusted subprocess. That is the whole interaction.

What it establishes is bounded and stated on every receipt: the observed responses to the cases in
the approved contract, from a snapshot of known byte identity, under a verified isolation policy.
Not universal correctness — an implementation that special-cases exactly these inputs passes, which
is why some cases are drawn from a controller-held seed. That raises the cost; it does not close it.
"""

from __future__ import annotations

import hashlib
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from cogos.verification.attestation import EvidenceAuthority
from cogos.verification.contract import AcceptanceContract
from cogos.verification.isolation import (
    HARNESS_MOUNT,
    SUBJECT_MOUNT,
    IsolationPolicy,
    IsolationUnavailable,
    IsolatedRun,
    probe_backend,
    run_isolated,
)
from cogos.verification.protocol import PROTOCOL_VERSION, READY_MARKER, SUBJECT_ADAPTER, Request, decode_transcript, sanitise

#: Bumping this changes what a receipt citing it means.
VERIFIER_VERSION = "cogos.behavioural.v1"

#: A snapshot larger than this is refused rather than partly bound. "Bound nothing" must never be
#: reachable by silence — that was the 500-file fail-open in a different costume.
MAX_SNAPSHOT_FILES = 2000
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
#: `ast.parse` on deeply nested input can exhaust the C stack, so the trusted parse is bounded and
#: the file it reads is capped first.
MAX_SOURCE_BYTES = 256 * 1024
STRUCTURAL_TIMEOUT_S = 20
STRUCTURAL_MEMORY_BYTES = 256 * 1024 * 1024


class SnapshotRefused(RuntimeError):
    """The immutable input could not be established. An explicit refusal, never a silent empty bind."""


@dataclass(frozen=True)
class SourceSnapshot:
    """A controller-owned copy of the deliverables, and its byte identity."""

    root: Path
    file_digests: dict[str, str]
    tree_digest: str

    def digest_of(self, relative: str) -> str:
        return self.file_digests.get(relative, "")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def materialise_snapshot(workspace: Path, contract: AcceptanceContract, destination: Path) -> SourceSnapshot:
    """Copy the declared deliverables into controller-owned storage and hash them.

    A read-only mount of the live workspace is not enough: another process on this host — the
    executive's own task tools included — can rewrite the backing file while the run is in flight,
    and the receipt would then name bytes that no longer produced the result. Copying first makes
    the input immutable for the life of the verification, and the digest is taken from the copy the
    subject actually ran against.
    """
    workspace = Path(workspace)
    destination.mkdir(parents=True, exist_ok=True)
    # The subject runs as uid 65534 and must be able to traverse and read what is mounted at
    # /subject. `mkdtemp` creates 0700, which the container user cannot enter — pytest then fails
    # inside `determine_setup` with PermissionError and the verifier reads that as the suite
    # failing. Readable-and-traversable, never writable: the mount is read-only regardless.
    os.chmod(destination, 0o755)
    digests: dict[str, str] = {}
    total = 0
    for relative in contract.deliverables:
        source = workspace / relative
        if source.is_symlink():
            # `is_file()` and `copyfile` both follow links, so a workspace containing only links to
            # files elsewhere on the host satisfied the existence predicate — and a link to a procfs
            # file reports st_size 0 while reading back thousands of bytes, which walks straight
            # through the size cap. The deliverable has to be in the workspace.
            raise SnapshotRefused(f"{relative} is a symbolic link; a deliverable must be a regular file in the workspace")
        if not source.is_file():
            # Absence is a fact about the deliverable, recorded rather than raised: the contract's
            # existence predicate is what reports it.
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # Copy with a hard cap on what is actually read, rather than trusting `st_size`: the
        # workspace is live, so the size that was checked and the bytes that arrive need not agree.
        written = 0
        with open(source, "rb") as src, open(target, "wb") as dst:
            while True:
                chunk = src.read(65536)
                if not chunk:
                    break
                written += len(chunk)
                total += len(chunk)
                if total > MAX_SNAPSHOT_BYTES:
                    dst.close()
                    target.unlink(missing_ok=True)
                    raise SnapshotRefused(f"the deliverables exceed {MAX_SNAPSHOT_BYTES} bytes; nothing was bound")
                dst.write(chunk)
        os.chmod(target, 0o444)
        digests[relative] = _sha256_file(target)

    tree = hashlib.sha256(json.dumps(digests, sort_keys=True).encode("utf-8")).hexdigest()
    return SourceSnapshot(root=destination, file_digests=digests, tree_digest=tree)


#: Directories that are never part of a deliverable and would only make the copy large.
_SKIP_DIRS = frozenset({".git", ".cogos", "__pycache__", ".pytest_cache", ".venv", "venv", "node_modules", ".tox", ".mypy_cache", ".ruff_cache"})


def materialise_workspace(workspace: Path, destination: Path) -> tuple[dict[str, str], list[str]]:
    """Copy the whole working tree for the suite runs, bounded, following no links.

    The suite runs used to see only the two declared deliverables, so a correct implementation whose
    tests are organised the ordinary way — a `conftest.py` fixture, a `pytest.ini`, a shared helper —
    failed on both the truth and the mutant run, and the human was told "a suite that does not tell
    them apart is not evidence about the implementation". That message blamed the suite for the
    verifier's missing files.

    The protocol run still sees only the deliverables; this is the wider copy the suite needs.
    """
    destination.mkdir(parents=True, exist_ok=True)
    os.chmod(destination, 0o755)
    digests: dict[str, str] = {}
    problems: list[str] = []
    total = files = 0
    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        relative_dir = Path(dirpath).relative_to(workspace)
        (destination / relative_dir).mkdir(parents=True, exist_ok=True)
        os.chmod(destination / relative_dir, 0o755)
        for name in sorted(filenames):
            source = Path(dirpath) / name
            relative = str(relative_dir / name) if str(relative_dir) != "." else name
            if source.is_symlink():
                problems.append(f"{relative} is a symbolic link and was not copied")
                continue
            if not source.is_file():
                continue
            files += 1
            if files > MAX_SNAPSHOT_FILES:
                raise SnapshotRefused(f"the workspace exceeds {MAX_SNAPSHOT_FILES} files; nothing was bound")
            target = destination / relative
            written = 0
            with open(source, "rb") as src, open(target, "wb") as dst:
                while True:
                    chunk = src.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_SNAPSHOT_BYTES:
                        dst.close()
                        target.unlink(missing_ok=True)
                        raise SnapshotRefused(f"the workspace exceeds {MAX_SNAPSHOT_BYTES} bytes; nothing was bound")
                    dst.write(chunk)
                    written += len(chunk)
            os.chmod(target, 0o444)
            digests[relative] = _sha256_file(target)
    return digests, problems


# -- the trusted structural read ---------------------------------------------------------

_STRUCTURAL_ANALYSER = r'''
import ast, json, sys

source = sys.stdin.read()
try:
    tree = ast.parse(source)
except BaseException as exc:
    print(json.dumps({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}))
    raise SystemExit(0)


def literal_number(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value)
    if (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant) and isinstance(node.operand.value, (int, float))):
        return -float(node.operand.value)
    return None


def sign(value):
    return "zero" if value == 0 else ("positive" if value > 0 else "negative")


def parametrize_of(fn):
    """(argnames, rows) for a pytest.mark.parametrize on this function, or None.

    The canonical way to write "tests covering positive, zero and negative percent" is one
    parametrized test. Counting bare `def test_*` only would reject the idiomatic suite as having
    one test and no percent literals, which is a false negative against correct work.
    """
    for dec in fn.decorator_list:
        call = dec if isinstance(dec, ast.Call) else None
        target = call.func if call else dec
        if getattr(target, "attr", None) != "parametrize" or call is None or len(call.args) < 2:
            continue
        names_node = call.args[0]
        if isinstance(names_node, ast.Constant) and isinstance(names_node.value, str):
            names = [n.strip() for n in names_node.value.replace(" ", "").split(",") if n.strip()]
        elif isinstance(names_node, (ast.List, ast.Tuple)):
            names = [n.value for n in names_node.elts if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        else:
            continue
        rows_node = call.args[1]
        rows = list(rows_node.elts) if isinstance(rows_node, (ast.List, ast.Tuple)) else []
        return names, rows
    return None


test_functions = []
cases = 0
signs = set()

for node in ast.walk(tree):
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith("test"):
        continue
    test_functions.append(node.name)
    params = parametrize_of(node)
    if params:
        names, rows = params
        cases += max(1, len(rows))
        index = names.index("percent") if "percent" in names else None
        for row in rows:
            elements = row.elts if isinstance(row, (ast.List, ast.Tuple)) else [row]
            if index is not None and index < len(elements):
                value = literal_number(elements[index])
                if value is not None:
                    signs.add(sign(value))
            else:
                # No column is named `percent`, so every literal in the row is a candidate. This is
                # approximate by construction and the check it feeds says only that percent-shaped
                # literals of each sign appear — never that the assertions are right.
                for element in elements:
                    value = literal_number(element)
                    if value is not None:
                        signs.add(sign(value))
    else:
        cases += 1

for node in ast.walk(tree):
    if not isinstance(node, ast.Call):
        continue
    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
    if name != "add_percent":
        continue
    argument = node.args[1] if len(node.args) >= 2 else None
    for keyword in node.keywords:
        if keyword.arg == "percent":
            argument = keyword.value
    value = literal_number(argument) if argument is not None else None
    if value is not None:
        signs.add(sign(value))

print(json.dumps({"ok": True, "test_functions": sorted(test_functions), "test_cases": cases, "percent_signs": sorted(signs)}))
'''


def _bounded_limits() -> None:  # pragma: no cover - runs in the forked child
    resource.setrlimit(resource.RLIMIT_AS, (STRUCTURAL_MEMORY_BYTES, STRUCTURAL_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (STRUCTURAL_TIMEOUT_S, STRUCTURAL_TIMEOUT_S))
    resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))


def read_structure(source_path: Path) -> dict[str, Any]:
    """Parse a deliverable's structure on the trusted side, bounded.

    Parsing is not executing: `ast.parse` builds a tree and runs nothing. It still gets its own
    process with hard memory and CPU limits, because a pathological nesting depth can take the
    interpreter down with it — and a verifier that can be crashed by its input is a verifier that
    can be silenced.
    """
    try:
        raw = source_path.read_bytes()
    except OSError as exc:
        return {"ok": False, "error": f"unreadable: {exc}"}
    if len(raw) > MAX_SOURCE_BYTES:
        return {"ok": False, "error": f"source exceeds {MAX_SOURCE_BYTES} bytes"}
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-S", "-c", _STRUCTURAL_ANALYSER],
            input=raw.decode("utf-8", "replace"),
            capture_output=True,
            text=True,
            timeout=STRUCTURAL_TIMEOUT_S,
            preexec_fn=_bounded_limits,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"structural read did not complete: {type(exc).__name__}"}
    if completed.returncode != 0 or not completed.stdout.strip():
        return {"ok": False, "error": f"structural read failed (exit {completed.returncode})"}
    try:
        return json.loads(completed.stdout.splitlines()[-1])
    except ValueError:
        return {"ok": False, "error": "structural read produced unparsable output"}


# -- the outcome --------------------------------------------------------------------------


@dataclass
class CaseOutcome:
    case_id: str
    args: dict[str, float]
    expected: float
    observed: Optional[float]
    passed: bool
    detail: str


@dataclass
class BehaviouralOutcome:
    """What the controller concluded, and everything a receipt needs to name."""

    satisfied: bool
    summary: str
    authority: EvidenceAuthority
    cases: list[CaseOutcome] = field(default_factory=list)
    structural: list[dict[str, Any]] = field(default_factory=list)
    existence: list[dict[str, Any]] = field(default_factory=list)
    suite: list[dict[str, Any]] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    refusal: str = ""

    @property
    def cases_passed(self) -> int:
        return sum(1 for c in self.cases if c.passed)


class BehaviouralVerifier:
    """Runs the approved contract against a workspace, inside the boundary, and decides."""

    def __init__(self, contract: AcceptanceContract, policy: Optional[IsolationPolicy] = None):
        self.contract = contract
        self.policy = policy or IsolationPolicy()

    def verify(self, workspace: Path, *, keep: bool = False) -> BehaviouralOutcome:
        backend = probe_backend()
        if not backend.available:
            # Refusal, not a verdict. The caller must not read this as evidence either way.
            return BehaviouralOutcome(
                satisfied=False,
                summary=f"the execution boundary is unavailable: {backend.missing}",
                authority=EvidenceAuthority.UNTRUSTED_SELF_REPORT,
                refusal=backend.missing,
                provenance={"backend": backend.as_dict()},
            )

        staging = Path(tempfile.mkdtemp(prefix="cogos-verify-"))
        os.chmod(staging, 0o700)
        try:
            snapshot_dir = staging / "snapshot"
            harness_dir = staging / "harness"
            harness_dir.mkdir()
            os.chmod(harness_dir, 0o755)
            # The adapter is mounted separately from the snapshot so no workspace file can shadow it.
            adapter = harness_dir / "adapter.py"
            adapter.write_text(SUBJECT_ADAPTER, encoding="utf-8")
            os.chmod(adapter, 0o444)
            suite_source = staging / "suite-source"
            try:
                snapshot = materialise_snapshot(Path(workspace), self.contract, snapshot_dir)
                # The suite needs whatever the deliverable's tests are organised around — a
                # conftest fixture, a pytest.ini, a helper module — not just the two declared files.
                suite_digests, suite_problems = materialise_workspace(Path(workspace), suite_source)
            except (SnapshotRefused, OSError) as exc:
                return BehaviouralOutcome(
                    satisfied=False,
                    summary=f"the source snapshot was refused: {exc}",
                    authority=EvidenceAuthority.UNTRUSTED_SELF_REPORT,
                    refusal=str(exc),
                    provenance={"backend": backend.as_dict()},
                )

            existence = [
                {
                    "deliverable": name,
                    "present": name in snapshot.file_digests,
                    "sha256": snapshot.digest_of(name),
                    "scope": "existence",
                }
                for name in self.contract.deliverables
            ]

            requests = [
                Request(request_id=uuid.uuid4().hex, op=case.op, args=case.args)
                for case in self.contract.cases
            ]
            stdin_data = "".join(request.encode() + "\n" for request in requests)

            try:
                run = run_isolated(
                    self.policy,
                    snapshot=snapshot.root,
                    harness=harness_dir,
                    argv=["python", "-I", "-P", f"{HARNESS_MOUNT}/adapter.py"],
                    stdin_data=stdin_data,
                    # Nothing reaches the subject's stdin until the adapter reports that it has
                    # imported the deliverable. Module-level code runs first, and a deliverable that
                    # read stdin at import time could answer the protocol itself — correct answers
                    # from a wrong `add_percent`.
                    handshake=READY_MARKER,
                    name_hint="cogos-behaviour",
                )
            except IsolationUnavailable as exc:
                return BehaviouralOutcome(
                    satisfied=False,
                    summary=f"the execution boundary could not be established: {exc}",
                    authority=EvidenceAuthority.UNTRUSTED_SELF_REPORT,
                    refusal=str(exc),
                    provenance={"backend": backend.as_dict()},
                )

            transcript = decode_transcript(run.stdout, [r.request_id for r in requests])
            cases: list[CaseOutcome] = []
            for request, case in zip(requests, self.contract.cases):
                response = transcript.responses.get(request.request_id)
                if response is None:
                    cases.append(CaseOutcome(case.case_id, case.args, case.expected, None, False, "no response"))
                elif not response.ok:
                    cases.append(CaseOutcome(case.case_id, case.args, case.expected, None, False, f"subject error: {sanitise(response.error, 120)}"))
                elif self.contract.matches(response.result or 0.0, case):
                    cases.append(CaseOutcome(case.case_id, case.args, case.expected, response.result, True, "matched"))
                else:
                    cases.append(CaseOutcome(case.case_id, case.args, case.expected, response.result, False, f"expected {case.expected}, observed {response.result}"))

            structural = self._structural(snapshot, extra_root=suite_source)
            suite = self._suite(snapshot, harness_dir, suite_source, suite_digests)

            missing_files = [e["deliverable"] for e in existence if not e["present"]]
            failed_cases = [c for c in cases if not c.passed]
            failed_structure = [s for s in structural if not s.get("passed")]
            failed_suite = [s for s in suite if not s.get("passed")]
            satisfied = not missing_files and not failed_cases and not failed_structure and not failed_suite and not transcript.problems

            if satisfied:
                summary = (
                    f"{len(cases)}/{len(cases)} behavioural cases matched the approved contract; "
                    f"{len(structural)} structural and {len(suite)} suite check(s) held; "
                    f"{len(existence)} deliverable(s) present"
                )
            else:
                parts = []
                if missing_files:
                    parts.append("missing deliverable(s): " + ", ".join(str(name) for name in missing_files))
                if failed_cases:
                    parts.append(f"{len(failed_cases)}/{len(cases)} behavioural case(s) did not match: " + "; ".join(c.detail for c in failed_cases[:3]))
                if failed_structure:
                    parts.append("structural: " + "; ".join(str(s.get("detail")) for s in failed_structure[:2]))
                if failed_suite:
                    parts.append("suite: " + "; ".join(str(s.get("detail")) for s in failed_suite[:2]))
                if transcript.problems:
                    parts.append("protocol: " + "; ".join(transcript.problems[:2]))
                summary = " | ".join(parts)

            return BehaviouralOutcome(
                satisfied=satisfied,
                summary=summary,
                # The controller compared, so the controller's conclusion is what carries. A failing
                # comparison is just as much its conclusion as a passing one.
                authority=EvidenceAuthority.TRUSTED_HARNESS,
                cases=cases,
                structural=structural,
                existence=existence,
                suite=suite,
                provenance=self._provenance(snapshot, run, backend, list(transcript.problems) + suite_problems, suite_digests),
            )
        finally:
            if not keep:
                shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        """Coerce a fact from the structural subprocess into the shape this code expects.

        The facts arrive as JSON from another process. Trusting the shape because we wrote the
        analyser is the same mistake as trusting a report because we chose its path.
        """
        return [str(item) for item in value] if isinstance(value, list) else []

    def _structural(self, snapshot: SourceSnapshot, extra_root: Optional[Path] = None) -> list[dict[str, Any]]:
        results: list[dict[str, object]] = []
        cache: dict[str, dict[str, object]] = {}
        for check in self.contract.structural:
            path = snapshot.root / check.path
            if check.path not in cache:
                cache[check.path] = read_structure(path) if path.is_file() else {"ok": False, "error": "file absent"}
            facts = cache[check.path]
            if not facts.get("ok"):
                results.append({"check_id": check.check_id, "passed": False, "scope": "structure", "detail": f"{check.path}: {facts.get('error')}"})
                continue
            if check.kind == "min_test_functions":
                minimum = int(check.detail.get("minimum", 1))
                found = self._string_list(facts.get("test_functions"))
                # Parametrized rows count as the cases they are: one `@parametrize` over three
                # percents is three cases, and rejecting it as "one test" fails correct work.
                observed = facts.get("test_cases")
                cases = int(observed) if isinstance(observed, int) else len(found)
                results.append({
                    "check_id": check.check_id,
                    "passed": cases >= minimum,
                    "scope": "structure",
                    "detail": f"{cases} test case(s) across {len(found)} function(s) in {check.path}, {minimum} required: {', '.join(found[:5])}",
                })
            elif check.kind == "percent_sign_coverage":
                required = set(self._string_list(check.detail.get("required")))
                found = set(self._string_list(facts.get("percent_signs")))
                # Fixtures are part of the suite's sources. A file named here is read the same way
                # and contributes the signs it exercises.
                for extra in self._string_list(check.detail.get("also_read")):
                    if extra not in cache:
                        extra_path = (extra_root or snapshot.root) / extra
                        cache[extra] = read_structure(extra_path) if extra_path.is_file() else {"ok": False, "error": "absent"}
                    if cache[extra].get("ok"):
                        found |= set(self._string_list(cache[extra].get("percent_signs")))
                missing = sorted(required - found)
                results.append({
                    "check_id": check.check_id,
                    "passed": not missing,
                    "scope": "structure",
                    # When nothing is found at all the honest reading is "not determinable from the
                    # sources", not "the suite omits these cases". A suite that keeps its cases in a
                    # fixture, a data file or a helper is ordinary, and no static reader can follow
                    # it there. The criterion still blocks — an unanswered predicate is not a
                    # satisfied one — but the reason says what actually happened.
                    "determinable": bool(found),
                    "detail": (
                        f"{check.path} exercises {sorted(found)} percent literal(s)" + (f"; missing {missing}" if missing else "")
                        if found else
                        f"percent coverage could not be determined from {check.path} or its fixtures: no percent literal is "
                        f"visible in the sources. The cases may be held in a fixture or data file, which no static reader "
                        f"can follow. This is an absence of evidence, not evidence the cases are missing."
                    ),
                })
        return results

    def _suite(self, snapshot: SourceSnapshot, harness_dir: Path, suite_source: Path, suite_digests: dict[str, str]) -> list[dict[str, Any]]:
        """Run the deliverable's own suite in the boundary, against the truth and against a mutant.

        Both runs happen inside the same isolation policy, on controller-owned copies. The exit
        codes come from the subject's process, so neither alone is trusted; what the controller
        decides is the *difference* between them, which a suite that asserts nothing cannot produce.
        """
        results: list[dict[str, object]] = []
        for check in self.contract.suite:
            if check.path not in suite_digests:
                results.append({"check_id": check.check_id, "passed": False, "scope": "suite_differential", "detail": f"{check.path} is absent"})
                continue
            outcomes: dict[str, Optional[int]] = {}
            failures: dict[str, str] = {}
            for label, mutate in (("truth", False), ("mutant", True)):
                variant = Path(tempfile.mkdtemp(prefix=f"cogos-suite-{label}-"))
                try:
                    os.chmod(variant, 0o755)
                    for name in suite_digests:
                        target = variant / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        os.makedirs(target.parent, exist_ok=True)
                        shutil.copyfile(suite_source / name, target)
                        os.chmod(target, 0o444)
                    for directory in {Path(n).parent for n in suite_digests if Path(n).parent != Path(".")}:
                        os.chmod(variant / directory, 0o755)
                    if mutate:
                        mutant = variant / "calc.py"
                        mutant.write_text(check.mutant_source, encoding="utf-8")
                        os.chmod(mutant, 0o444)
                    try:
                        run = run_isolated(
                            self.policy,
                            snapshot=variant,
                            harness=harness_dir,
                            argv=["python", "-I", "-P", "-m", "pytest", "-q", "-p", "no:cacheprovider", SUBJECT_MOUNT],
                            name_hint=f"cogos-suite-{label}",
                        )
                    except IsolationUnavailable as exc:
                        results.append({"check_id": check.check_id, "passed": False, "scope": "suite_differential", "detail": f"boundary unavailable: {exc}"})
                        outcomes = {}
                        break
                    outcomes[label] = run.exit_code
                    if run.failure:
                        failures[label] = run.failure
                finally:
                    shutil.rmtree(variant, ignore_errors=True)
            if not outcomes:
                continue
            truth, mutant = outcomes.get("truth"), outcomes.get("mutant")
            # Exit 125 is docker refusing to start the container, which `run_isolated` already
            # identifies as a backend failure rather than the subject failing. Counting it as
            # discrimination turned an infrastructure hiccup on the mutant run into a passing suite
            # check for a suite that asserts nothing — a fail-open on the one check meant to catch
            # exactly that.
            infrastructure = {label: code for label, code in outcomes.items() if code == 125 or failures.get(label)}
            discriminates = truth == 0 and mutant not in (0, None, 125) and not infrastructure
            results.append({
                "check_id": check.check_id,
                "passed": discriminates,
                "scope": "suite_differential",
                "detail": f"suite exit {truth} against the deliverable, exit {mutant} against a controller-substituted wrong implementation"
                + ("" if discriminates else (
                    f"; the run did not complete cleanly ({infrastructure}), so nothing was established either way"
                    if infrastructure else
                    "; a suite that does not tell them apart is not evidence about the implementation")),
            })
        return results

    def _provenance(self, snapshot: SourceSnapshot, run: IsolatedRun, backend, problems: list[str], suite_digests: Optional[dict[str, str]] = None) -> dict[str, Any]:
        """Everything a receipt must name so the result can be re-derived or refuted later."""
        return {
            "verifier": VERIFIER_VERSION,
            "protocol": PROTOCOL_VERSION,
            "contract_version": self.contract.contract_version,
            "contract_digest": self.contract.digest(),
            "requirements_digest": self.contract.requirements_digest,
            "snapshot_tree_digest": snapshot.tree_digest,
            "snapshot_files": dict(snapshot.file_digests),
            "suite_workspace_files": dict(suite_digests or {}),
            "image": self.policy.image,
            "isolation_policy_digest": self.policy.digest(),
            "isolation_policy": asdict(self.policy),
            "backend": backend.as_dict(),
            "execution": run.facts(),
            "protocol_problems": list(problems),
            "mutant_digests": {c.check_id: hashlib.sha256(c.mutant_source.encode("utf-8")).hexdigest() for c in self.contract.suite},
        }
