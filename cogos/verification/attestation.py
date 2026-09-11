"""Evidence authority, the trusted verifier path, and the engine's differential control.

**The claim this module exists to withdraw.** The previous repair rested on: *"a report at a path
the engine chose cannot be produced by a runner that never ran."* That is false. The path is handed
to the child in its own argv, the child runs as the same uid, and it can write there. Reproduced
end to end: a workspace ``pytest.py`` that reads ``--junitxml=`` out of ``sys.argv`` and writes
plausible passing XML carried an implementation returning ``999.0`` through every stage to
``mission_completion_check`` = PASSED.

**The generalisation.** Any process that imports the code under test can author that process's own
report, stdout and exit status. A random filename, a nonce passed to the child, a report hash, a
trusted launcher and the exit code are each defeated, because each is something the child is either
told or asked to produce. The strongest case defeats all of them at once: a ``conftest.py``
hookwrapper on ``pytest_runtest_makereport`` flips outcomes, and then the *genuine* runner writes a
genuine, well-formed, correctly located, freshly stamped report whose contents are false.

So this module does two separate things, and neither is "check another attribute of the XML".

1. **A trusted verifier path.** The engine stops accepting a model-authored command and builds the
   invocation itself: its own interpreter, its own flags, its own configuration file outside the
   workspace, ambient plugin and config discovery disabled, a scrubbed environment, the workspace
   off the import path, no shell, a selection it validates against the tree, and an evidence
   destination in a private directory that did not exist beforehand. Measured: this neutralises
   the ``pytest.py`` shadow, ``pytest.ini`` ``addopts`` plugin injection and ``PYTEST_ADDOPTS``
   in the ambient environment *at the source* — the real runner runs and reports the real
   failures.
2. **A differential control.** What the trusted path cannot do is stop workspace code that the
   run legitimately imports — ``conftest.py``, plugins the tests import, the test modules
   themselves — from manipulating results in process. No property of the report distinguishes
   that case. So the engine asks a question the workspace does not get to answer by writing a
   file: *does this result depend on the implementation?* It re-runs the same trusted invocation
   itself against a private copy of the workspace whose implementation modules have been replaced
   by stubs that import but raise on use. A pass that survives the implementation being withheld
   was never a measurement of it.

**What this is worth, stated plainly — and what it is not.** The trusted path closes the
configuration, plugin, environment and import-path routes *at the source*: measured, the
``pytest.py`` shadow, ``addopts = -p <plugin>`` in the workspace ini and ``PYTEST_ADDOPTS`` in the
ambient environment all stop working, and the real runner reports the real failures. **It is not a
boundary.** The subject's code still executes inside the process that reports on it, and from
there it can rewrite pytest's internals, unregister the plugin that writes the evidence, mutate
result objects, read the report path out of its own argv, overwrite the file, and call
``os._exit(0)``. ``--noconftest`` does not change that: the test modules are subject code too, and
an attack simply moves into one — measured, with conftest loading disabled a failing suite still
produced a passing report.

The differential control catches every such forgery reproduced here, including the ones the
trusted path cannot touch. It is still not a boundary: it is a *sensitivity check*, and an
adversary who models it and answers the two runs differently defeats it.

So same-process verification of adversarial subject code is **not supported** by this runtime, and
the runtime says so rather than papering over it. With ``governance.trust_workspace_code`` False —
the default — an in-process result is capped at :attr:`EvidenceAuthority.PROCESS_OBSERVATION`,
which cannot close a behavioural criterion or pass the completion gate, however green the run.
Setting it True is a human's statement that a particular workspace's code is not targeting the
verifier; it never disables the harness or the control, it only lets their result carry authority.
:attr:`EvidenceAuthority.TRUSTED_HARNESS` therefore means "produced through the engine's own
invocation, shown to depend on the implementation, in a workspace a human declared
non-adversarial" — never "authenticated".

The missing primitive is a runner outside the engine's trust domain: a separate uid, a read-only
workspace mount, and an evidence channel the subject process cannot write. This task does not
build one, and until it exists the honest classification of adversarial same-process verification
is *unsupported*.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Sequence


class EvidenceAuthority(str, Enum):
    """How much a piece of evidence is allowed to prove.

    Ordered. A criterion about behaviour requires at least :attr:`TRUSTED_HARNESS`; everything
    below is retained as diagnosis and closes nothing.
    """

    #: Numbers a process reported about itself: stdout, an exit code, a JUnit file written by a
    #: process that imports the code under test. Also the value every legacy or unlabelled record
    #: deserialises to — an absent field is indistinguishable from a stripped one.
    UNTRUSTED_SELF_REPORT = "untrusted_self_report"
    #: Facts the engine observed about the process rather than from it: that it was started with a
    #: given invocation, that it terminated, when. True, and not evidence about tests.
    PROCESS_OBSERVATION = "process_observation"
    #: Produced through the engine's own invocation *and* shown by the engine's differential
    #: control to depend on the implementation. The strongest level this runtime can establish.
    TRUSTED_HARNESS = "trusted_harness"
    #: Established outside this runtime entirely — an isolated CI re-run, or a person. Nothing in
    #: this runtime produces it; it exists so that external validation has somewhere to land.
    INDEPENDENT_VALIDATION = "independent_validation"


_ORDER = {
    EvidenceAuthority.UNTRUSTED_SELF_REPORT: 0,
    EvidenceAuthority.PROCESS_OBSERVATION: 1,
    EvidenceAuthority.TRUSTED_HARNESS: 2,
    EvidenceAuthority.INDEPENDENT_VALIDATION: 3,
}

#: The floor for closing a criterion about behaviour, or for grounding a judgement about one.
BEHAVIOURAL_FLOOR = EvidenceAuthority.TRUSTED_HARNESS


def authority_of(value: object) -> EvidenceAuthority:
    """Deserialise a stored authority, defaulting **down**.

    An absent or unrecognised value is :attr:`EvidenceAuthority.UNTRUSTED_SELF_REPORT`. The
    permissive reading — "written before authority existed, keep the older guarantee" — reopens
    every hole the level was introduced to close, because a missing field is indistinguishable
    from a stripped field and from a record produced under the rules this replaced.
    """
    if isinstance(value, EvidenceAuthority):
        return value
    try:
        return EvidenceAuthority(str(value or ""))
    except ValueError:
        return EvidenceAuthority.UNTRUSTED_SELF_REPORT


def at_least(value: object, floor: EvidenceAuthority = BEHAVIOURAL_FLOOR) -> bool:
    return _ORDER[authority_of(value)] >= _ORDER[floor]


#: What `verify_artifact` established. `existence` is a path resolving to a regular non-empty file
#: with a recorded hash — integrity, never content. `content` is added only when the mission
#: declared an expectation in advance and the engine checked the bytes against it.
EXISTENCE_SCOPE = "existence"
CONTENT_SCOPE = "content"
#: Added when the isolated behavioural verifier decided this file against the approved contract —
#: a claim about what the file *does*, which is strictly more than what it contains. Kept as a
#: separate token so that every existing consumer keying on `content` keeps working unchanged.
BEHAVIOUR_SCOPE = "behaviour"

#: Frameworks the engine has a trusted verifier path for. A framework outside this set cannot
#: produce evidence above `UNTRUSTED_SELF_REPORT` here, whatever its output looks like.
REPORTING_FRAMEWORKS = frozenset({"pytest"})
#: Frameworks the command parser recognises. Recognised-but-not-supported means the run is
#: diagnostic: `unittest` and `nose` have no trusted path, and a model-authored command naming one
#: is not evidence that a supported runner produced anything.
KNOWN_FRAMEWORKS = frozenset({"pytest", "unittest", "nose"})

_SKIP_DIRS = frozenset({".git", ".hg", ".svn", ".cogos", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".eggs"})
_TEST_NAME = re.compile(r"^(test_.*|.*_test|conftest)\.py$")

#: Bounds on the private copy the control runs in. A workspace past these cannot be controlled,
#: which is reported as an absence of attestation and never as a pass.
MAX_CONTROL_FILES = 3000
MAX_CONTROL_BYTES = 64 * 1024 * 1024
CONTROL_TIMEOUT_S = 300

#: Environment variables that let something outside the invocation change what the runner does.
#: Scrubbed rather than trusted: `PYTEST_ADDOPTS=-p forge` in the ambient environment injected a
#: forging plugin into an otherwise clean run.
_SCRUB_ENV = ("PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DEBUG", "PYTEST_CURRENT_TEST")

#: An implementation stub that still imports and still exposes every name, so the test modules
#: collect exactly as before and their identities appear in the control report — but that raises
#: the moment anything it exposes is used. Replacing the module with a bare `raise` instead turns
#: the control into a collection error, and a collection error hides the identities the
#: comparison needs.
_STUB = (
    "# cogos attestation control: this module was replaced by the verification engine.\n"
    "class _CogosControlStub:\n"
    "    def __call__(self, *a, **k):\n"
    "        raise RuntimeError('cogos attestation control: implementation withheld')\n"
    "    def __getattr__(self, name):\n"
    "        raise RuntimeError('cogos attestation control: implementation withheld')\n"
    "def __getattr__(name):\n"
    "    if name.startswith('__') and name.endswith('__'):\n"
    "        raise AttributeError(name)\n"
    "    return _CogosControlStub()\n"
)


# -- the trusted verifier path ----------------------------------------------------------


@dataclass
class TrustedRun:
    """An invocation the engine authored, and the private place its evidence lands."""

    argv: list[str]
    env: dict[str, str]
    cwd: Path
    report: Path
    #: Removed by :meth:`dispose`. Holds the engine's config file and the report, outside the
    #: workspace and created fresh, so neither can be pre-written or left over from a prior run.
    private_dir: Path
    selection: list[str]

    def dispose(self) -> None:
        shutil.rmtree(self.private_dir, ignore_errors=True)


_FLAGLESS = re.compile(r"^-")


def harness_selection(command: str, cwd: Path) -> list[str]:
    """The test paths a model-authored command asked for, keeping only ones that exist.

    Selection is the one thing the model still influences, because only the plan knows which part
    of the suite a criterion is about. It is bounded on both sides: a token has to resolve to a
    real path inside the directory under test, and the report's identities are checked back
    against what was selected.
    """
    out: list[str] = []
    for token in (command or "").split()[1:]:
        if _FLAGLESS.match(token) or "=" in token:
            continue
        base = token.split("::", 1)[0]
        try:
            candidate = (cwd / base).resolve()
            candidate.relative_to(cwd.resolve())
        except (OSError, ValueError):
            continue
        if candidate.exists():
            out.append(token)
    return out


def trusted_env() -> dict[str, str]:
    """The environment the trusted verifier path runs in.

    Built here rather than passed through a tool call, so that "run with a controlled
    environment" is a property of the path and not a new argument any caller could set.
    """
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB_ENV}
    # The workspace must not be on the import path: otherwise a module named `pytest.py` there
    # *is* the runner under `python -m pytest`.
    env["PYTHONSAFEPATH"] = "1"
    # No third-party plugin may load itself into the run without being named in the invocation.
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["COGOS_TOOL"] = "1"
    return env


def build_trusted_run(*, cwd: Path, selection: Sequence[str], interpreter: Optional[str] = None) -> TrustedRun:
    """Construct the invocation the engine will run, controlling everything it can control.

    Controlled here: the executable (this runtime's interpreter, not a path from the plan), the
    working directory, the environment, the import path, the configuration file, plugin
    autoloading, the cache plugin, the test selection, and the evidence destination. Not
    controlled, because it cannot be: the code the run imports.
    """
    private = Path(tempfile.mkdtemp(prefix="cogos-verify-"))
    os.chmod(private, 0o700)
    config = private / "pytest.ini"
    # An engine-authored config outside the workspace. With `-c`, pytest ignores the workspace's
    # own ini entirely, which is what closes `addopts = -p <forging plugin>` at the source.
    config.write_text("[pytest]\n", encoding="utf-8")
    report = private / "junit.xml"
    env = trusted_env()
    argv = [
        interpreter or sys.executable,
        "-P",
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "-c",
        str(config),
        "--rootdir",
        str(cwd),
        f"--junitxml={report}",
    ]
    argv.extend(selection or [str(cwd)])
    return TrustedRun(argv=argv, env=env, cwd=cwd, report=report, private_dir=private, selection=list(selection))


# -- report parsing, identity and integrity ---------------------------------------------


@dataclass(frozen=True)
class TestCaseId:
    classname: str
    name: str
    outcome: str  # passed | failed | error | skipped

    @property
    def key(self) -> str:
        return f"{self.classname}::{self.name}" if self.classname else self.name


@dataclass(frozen=True)
class JUnitReport:
    counts: dict[str, int]
    cases: tuple[TestCaseId, ...]
    files: tuple[str, ...]
    problems: tuple[str, ...] = ()

    def passed_keys(self) -> set[str]:
        return {c.key for c in self.cases if c.outcome == "passed"}


def _case_outcome(node: ET.Element) -> str:
    if node.find("error") is not None:
        return "error"
    if node.find("failure") is not None:
        return "failed"
    if node.find("skipped") is not None:
        return "skipped"
    return "passed"


def _case_file(node: ET.Element) -> str:
    explicit = node.get("file")
    if explicit:
        return explicit
    module = (node.get("classname") or node.get("name") or "").split("::", 1)[0]
    return module.replace(".", "/") + ".py" if module else ""


def parse_junit(report: Path) -> Optional[JUnitReport]:
    """Parse a JUnit report into counts *and* identities, listing its integrity problems.

    Returns None only when there is no usable XML. A parsable report whose header disagrees with
    its own testcase elements comes back *with* its problems listed, so the caller can say why it
    was refused instead of silently behaving as though no report existed.
    """
    try:
        if not report.is_file() or report.stat().st_size == 0:
            return None
        root = ET.parse(report).getroot()
    except (OSError, ET.ParseError):
        return None
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        return None

    total = failures = errors = skipped = 0
    cases: list[TestCaseId] = []
    files: set[str] = set()
    problems: list[str] = []
    for suite in suites:
        try:
            total += int(suite.get("tests", 0) or 0)
            failures += int(suite.get("failures", 0) or 0)
            errors += int(suite.get("errors", 0) or 0)
            skipped += int(suite.get("skipped", 0) or 0)
        except ValueError:
            return None
        for node in suite.iter("testcase"):
            cases.append(TestCaseId(node.get("classname", "") or "", node.get("name", "") or "", _case_outcome(node)))
            f = _case_file(node)
            if f:
                files.add(f)

    # Internal consistency. A header declaring nine tests over a single testcase element describes
    # a run that did not happen; reproduced, it satisfied a criterion and passed the completion
    # gate on the strength of the header alone.
    if total != len(cases):
        problems.append(f"header declares {total} test(s) but the report carries {len(cases)} testcase element(s)")
    seen: set[str] = set()
    dupes: set[str] = set()
    for c in cases:
        if c.key in seen:
            dupes.add(c.key)
        seen.add(c.key)
    if dupes:
        problems.append(f"duplicate test identities: {', '.join(sorted(dupes)[:3])}")
    for kind, header, actual in (
        ("failures", failures, sum(1 for c in cases if c.outcome == "failed")),
        ("errors", errors, sum(1 for c in cases if c.outcome == "error")),
        ("skipped", skipped, sum(1 for c in cases if c.outcome == "skipped")),
    ):
        if header != actual:
            problems.append(f"header declares {header} {kind} but {actual} testcase element(s) say so")

    passed = max(0, total - failures - errors - skipped)
    return JUnitReport(
        counts={"passed": passed, "failed": failures, "error": errors, "skipped": skipped},
        cases=tuple(cases),
        files=tuple(sorted(files)),
        problems=tuple(problems),
    )


def scope_problems(report: JUnitReport, cwd: Optional[Path], selection: Sequence[str] = ()) -> list[str]:
    """Ways the report describes something other than what was asked for.

    A report naming an entirely different suite satisfied a criterion about this one and passed
    the gate, so the identities have to be checked back against the tree and the selection.
    """
    if cwd is None:
        return []
    out: list[str] = []
    missing = [f for f in report.files if f and not (cwd / f).exists()]
    if missing:
        out.append(f"report names test file(s) absent from the directory under test: {', '.join(missing[:3])}")
    chosen = [s.split("::", 1)[0] for s in selection if not s.startswith("-")]
    if chosen and report.files:
        roots = [str(Path(c)) for c in chosen]
        stray = [f for f in report.files if not any(f == r or f.startswith(r.rstrip("/") + "/") for r in roots)]
        if stray:
            out.append(f"report names test file(s) outside the selected target: {', '.join(stray[:3])}")
    return out


def read_report_once_settled(report: Path, not_before: float) -> tuple[Optional[JUnitReport], list[str]]:
    """Read the report, refusing one that predates the run or changes while being read.

    Freshness is checked against the moment the engine launched the run, and the bytes are read
    twice: a process that outlives the runner and rewrites the file in the window between the
    runner exiting and the engine reading would otherwise land its replacement unnoticed.
    """
    try:
        if not report.is_file():
            return None, []
        if report.stat().st_mtime < not_before - 1.0:
            return None, ["the report predates the run that was supposed to produce it"]
        first = report.read_bytes()
    except OSError:
        return None, []
    parsed = parse_junit(report)
    if parsed is None:
        return None, []
    try:
        second = report.read_bytes()
    except OSError:
        return None, ["the report disappeared while it was being read"]
    if first != second:
        return None, ["the report changed while it was being read"]
    return parsed, list(parsed.problems)


# -- the differential control -----------------------------------------------------------


@dataclass(frozen=True)
class ControlOutcome:
    attested: bool
    reason: str
    detail: str = ""


def implementation_candidates(root: Path) -> list[Path]:
    """Non-test Python modules in the workspace: what the tests are supposed to be about."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in sorted(filenames):
            if name.endswith(".py") and not _TEST_NAME.match(name):
                out.append(Path(dirpath) / name)
    return out


def _copy_workspace(src: Path, dst: Path) -> Optional[str]:
    files = 0
    size = 0
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        rel = Path(dirpath).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for name in filenames:
            s = Path(dirpath) / name
            # `is_file` follows symlinks, so a symlinked module is copied as the bytes it points
            # at — which is what the run under test imported. Anything that is not a regular file
            # after following (a device, a dangling link) is left out.
            if not s.is_file():
                continue
            files += 1
            try:
                size += s.stat().st_size
            except OSError:
                continue
            if files > MAX_CONTROL_FILES or size > MAX_CONTROL_BYTES:
                return f"workspace exceeds the control bounds ({MAX_CONTROL_FILES} files / {MAX_CONTROL_BYTES // (1024 * 1024)} MiB)"
            try:
                shutil.copy2(s, dst / rel / name)
            except OSError as exc:
                return f"could not copy {rel / name}: {exc}"
    return None


def differential_control(*, cwd: Path, selection: Sequence[str], passed_keys: set[str]) -> ControlOutcome:
    """Re-run the trusted invocation against a workspace whose implementation is withheld.

    The engine runs this itself rather than through the tool fabric: it is the runtime deciding
    whether to believe evidence, not the executive taking an action, so it must neither consume
    nor depend on the executive's authorization. It re-executes an invocation the engine itself
    authored, in a private copy, granting no authority that was not already exercised.

    Attested only when the control report contains **every** identity that passed for real and
    none of them passes without the implementation. Anything else — no report, a collection error
    that hides the identities, a copy that could not be made — is an absence of attestation, and
    absence is never read as a pass.
    """
    if not passed_keys:
        return ControlOutcome(False, "no passing test identity to control for")
    base = Path(tempfile.mkdtemp(prefix="cogos-control-"))
    run: Optional[TrustedRun] = None
    try:
        os.chmod(base, 0o700)
        work = base / "w"
        work.mkdir()
        err = _copy_workspace(cwd, work)
        if err:
            return ControlOutcome(False, "the control workspace could not be prepared", err)
        candidates = implementation_candidates(work)
        if not candidates:
            return ControlOutcome(False, "no implementation module to withhold: the run depends on no non-test code in the directory under test")
        for path in candidates:
            try:
                path.write_text(_STUB, encoding="utf-8")
            except OSError as exc:
                return ControlOutcome(False, "the control workspace could not be prepared", str(exc))
        rebased = [s if not (work / s.split("::", 1)[0]).exists() else s for s in selection]
        run = build_trusted_run(cwd=work, selection=rebased)
        try:
            subprocess.run(run.argv, cwd=str(work), env=run.env, capture_output=True, text=True, timeout=CONTROL_TIMEOUT_S, check=False)
        except (subprocess.TimeoutExpired, OSError) as exc:
            return ControlOutcome(False, "the control run did not complete", str(exc))
        parsed = parse_junit(run.report)
        if parsed is None:
            return ControlOutcome(False, "the control run produced no machine-readable report, so the result could not be compared")
        control_keys = {c.key for c in parsed.cases}
        absent = sorted(passed_keys - control_keys)
        if absent:
            return ControlOutcome(
                False,
                "the control run did not report on every test that passed, so the passes could not be attributed",
                f"absent from the control report: {', '.join(absent[:3])}",
            )
        survivors = sorted(passed_keys & parsed.passed_keys())
        if survivors:
            return ControlOutcome(
                False,
                "test(s) still reported as passing with the implementation withheld: the result does not depend on the code under test",
                f"survived: {', '.join(survivors[:3])}",
            )
        return ControlOutcome(True, f"all {len(passed_keys)} passing test(s) stop passing when the implementation is withheld")
    finally:
        if run is not None:
            run.dispose()
        shutil.rmtree(base, ignore_errors=True)
