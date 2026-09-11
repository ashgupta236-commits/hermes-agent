"""Structured classification of one test-command run, and input-version identity.

Live Run #2 recorded a passing test verification for a command that executed no tests at all
(``ver_1m26hx9rc493b844d``: "1/1 commands passed ...; tests: 0 passed, 0 failed, 0 errors"). The
cause was that the engine read only the process exit code: ``if res.ok: status = PASSED``. Exit 0
means "the process did not fail", which is a different proposition from "the required tests ran
and passed".

This module answers the narrower question from structured runner output, and lives outside the
engine so it can be unit-tested against representative outputs rather than only end to end.

The rule it enforces: **execution has to be observed, not inferred.** A run that executed nothing
is unproven (INCONCLUSIVE), never proof. The single exception is an expected-zero run, and it is
only honoured when the verification contract declared it *before* the command ran — the executive
cannot look at an empty result and decide afterwards that empty was what it meant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional

from cogos.schemas.common import VerificationStatus
from cogos.verification.attestation import parse_junit

PASSED = VerificationStatus.PASSED
FAILED = VerificationStatus.FAILED
INCONCLUSIVE = VerificationStatus.INCONCLUSIVE

#: pytest's exit code for "no tests were collected". It is not a failure and not a success.
PYTEST_NO_TESTS_COLLECTED = 5

_NO_TESTS = re.compile(r"\bno tests ran\b|\bno tests collected\b|\bcollected 0 items\b", re.IGNORECASE)
_COLLECTION_ERROR = re.compile(
    r"\berrors? during collection\b|\bERROR collecting\b|\bINTERNALERROR\b|\bImportError while importing test module\b",
    re.IGNORECASE,
)
_COLLECTED = re.compile(r"\bcollected (\d+) items?\b", re.IGNORECASE)

COUNT_KEYS = ("passed", "failed", "error", "skipped")


@dataclass(frozen=True)
class TestRunOutcome:
    """What a test command actually demonstrated."""

    status: VerificationStatus
    reason: str
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    collected: Optional[int] = None
    exit_code: Optional[int] = None
    collection_error: bool = False
    expected_zero: bool = False
    framework: str = "unknown"
    command: str = ""

    @property
    def executed(self) -> int:
        """Tests that actually ran a body. Skipped tests are collected, not executed."""
        return self.passed + self.failed + self.errors

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "reason": self.reason,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "skipped": self.skipped,
            "executed": self.executed,
            "collected": self.collected,
            "exit_code": self.exit_code,
            "collection_error": self.collection_error,
            "expected_zero": self.expected_zero,
            "framework": self.framework,
        }


_PYTEST_SUMMARY = re.compile(
    r"(?:^|\n)(?:=+ )?(?P<summary>\d+ (?:passed|failed|error|errors|skipped|xfailed|xpassed|deselected)[^\n=]*?) in [\d.]+s",
)
_PYTEST_COUNTS = re.compile(r"(\d+) (passed|failed|error|errors|skipped|xfailed|xpassed)")
_RUNNER_IN_COMMAND = re.compile(r"(?:^|\s)(pytest|py\.test)\b|-m\s+pytest\b|-m\s+unittest\b|(?:^|\s)nose2?\b")
#: Anything that lets a second program write to the same stdout, or redirect the runner's away.
_COMPOSED = re.compile(r"[;&|><`]|\$\(|\n")


def detect_framework(command: str) -> str:
    """Identify the runner from a command whose output is attributable to it, or return unknown.

    Two separate problems have to be closed here, and closing only one leaves the hole open.

    Output is written by the process under test, so a program printing "5 passed" could forge
    evidence about itself — hence reading the *command* rather than the output. But the command is
    model-authored, and the firewall classifies danger, not truthfulness: verified against this
    module's own earlier version, ``pytest -q > /dev/null 2>&1; echo "1 passed in 0.02s"`` named a
    real runner, discarded its real (failing) output, and printed a convincing summary — producing
    a PASSED record with one fabricated test while the suite actually failed.

    So the command must also be *simple*: one invocation, nothing chained, piped or redirected.
    Counts can only be attributed to a runner when the runner is the only thing that wrote them.
    A composed command still executes — it is an authorized action — but its stdout is not test
    evidence.
    """
    text = command or ""
    if _COMPOSED.search(text):
        return "unknown"
    if "--junitxml" in text or "--report-log" in text:
        # The report path is the one thing the runtime must choose. A command that picks its own
        # could pre-write it, so its output is not attributable either.
        return "unknown"
    m = _RUNNER_IN_COMMAND.search(text)
    if not m:
        return "unknown"
    found = m.group(0)
    if "unittest" in found:
        return "unittest"
    if "nose" in found:
        return "nose"
    return "pytest"


def junit_counts(report: Path) -> Optional[dict[str, int]]:
    """Counts from a JUnit XML report, or None when there is no usable XML.

    **Correction.** An earlier version of this docstring claimed that "a report at a path the
    engine chose cannot be produced by a runner that never ran". That claim was false and is
    withdrawn. An engine-selected output path does not authenticate the producer: the path is
    handed to the child in its own argv, the child runs as the same uid, and it can write there.
    Reproduced end to end — a workspace ``pytest.py`` reading ``--junitxml=`` out of ``sys.argv``
    and writing passing XML carried an implementation returning ``999.0`` to a PASSED completion
    gate.

    Counts alone therefore carry no authority. What the engine can establish about a run lives in
    :mod:`cogos.verification.attestation`; this function is the numeric half of that parse, kept
    for callers that only want totals.
    """
    parsed = parse_junit(report)
    return dict(parsed.counts) if parsed is not None else None


def summary_lines(output: str) -> list[str]:
    """Every runner-summary-shaped line in the output.

    More than one means the result is not attributable. The code under test writes to the same
    stream the runner does, so a ``conftest.py`` that prints ``3 passed in 0.12s`` puts a second
    summary in front of the real one — verified: a suite whose only test was *skipped* reported
    three passing tests that way.
    """
    return [m.group("summary").strip("= ") for m in _PYTEST_SUMMARY.finditer(output or "")]


def parse_test_output(output: str) -> dict[str, int]:
    """Counts from a runner's summary line.

    Isolated here (rather than inline in the subprocess handler) so it can be tested against
    representative outputs, and anchored on the runner's ``N <kind> in <duration>s`` summary form
    so that a stray "5 passed" elsewhere in a log is not mistaken for a result line.

    When the output carries more than one summary line the counts are not attributable to the
    runner, so nothing is reported: :func:`classify_test_run` turns that into INCONCLUSIVE rather
    than picking one of them.
    """
    counts = {k: 0 for k in COUNT_KEYS}
    lines = summary_lines(output)
    if len(lines) != 1:
        return counts
    for num, kind in _PYTEST_COUNTS.findall(lines[0]):
        key = "error" if kind.startswith("error") else kind
        if key in counts:
            counts[key] += int(num)
    return counts


def summarise_test_output(output: str, fallback: str) -> str:
    m = _PYTEST_SUMMARY.search(output or "")
    if m:
        return m.group("summary").strip("= ")
    tail = (output or "").strip().splitlines()
    return tail[-1] if tail else fallback


def classify_test_run(
    *,
    exit_code: Optional[int],
    counts: Optional[Mapping[str, int]] = None,
    output: str = "",
    command: str = "",
    expect_zero: bool = False,
    report_counts: Optional[Mapping[str, int]] = None,
    report_required: bool = False,
) -> TestRunOutcome:
    """Decide what a single test-command run proves.

    ``expect_zero`` must come from the verification contract as declared before execution.
    """
    # A runner-written report at an engine-chosen path outranks anything printed to stdout.
    report_backed = report_counts is not None
    source: Mapping[str, int] = report_counts if report_counts is not None else (counts or {})
    c = {k: int(source.get(k, 0) or 0) for k in COUNT_KEYS}
    framework = detect_framework(command)
    # Unconditional, and deliberately not `(not report_backed) and ...`. Making the report
    # suppress this guard meant an attacker who fabricated a report was strictly better off than
    # one who did not: the forged report both supplied the counts and switched off the check that
    # would have noticed a second summary line. A second summary means something other than the
    # runner wrote one, and that is true whether or not a report also exists.
    ambiguous = len(summary_lines(output or "")) > 1
    collected_m = _COLLECTED.search(output or "")
    collected = int(collected_m.group(1)) if collected_m else None
    collection_error = bool(_COLLECTION_ERROR.search(output or ""))

    def outcome(status: VerificationStatus, reason: str) -> TestRunOutcome:
        return TestRunOutcome(
            status=status,
            reason=reason,
            passed=c["passed"],
            failed=c["failed"],
            errors=c["error"],
            skipped=c["skipped"],
            collected=collected,
            exit_code=exit_code,
            collection_error=collection_error,
            expected_zero=expect_zero,
            framework=framework,
            command=command,
        )

    # A collection error means the suite could not even be assembled: nothing was proven, and it
    # is a defect in the thing under test rather than an absence of evidence about it.
    if collection_error:
        return outcome(FAILED, "tests could not be collected")
    if ambiguous:
        # Two or more summary lines: something besides the runner wrote one of them, and there is
        # no honest way to pick. A real failure still fails below on the exit code; what cannot
        # happen is reading this as a pass.
        c["passed"] = c["failed"] = c["error"] = c["skipped"] = 0
        if exit_code not in (0, None):
            return outcome(FAILED, f"command exited {exit_code}; output carries multiple runner summaries")
        return outcome(INCONCLUSIVE, "output carries more than one runner summary: the result is not attributable")
    if c["failed"] or c["error"]:
        return outcome(FAILED, f"{c['failed']} failed, {c['error']} error(s)")

    if report_required and not report_backed and framework != "unknown":
        # The runner was asked for a machine-readable report and produced none, so whatever wrote
        # that stdout was not the runner we asked for.
        c["passed"] = c["failed"] = c["error"] = c["skipped"] = 0
        if expect_zero and exit_code in (0, None):
            return outcome(PASSED, "zero executed tests, explicitly authorized by the verification contract")
        return outcome(INCONCLUSIVE, "no machine-readable report was produced: the output is not attributable to the runner")
    if framework == "unknown":
        # Either the command invoked no recognised runner, or it was composed so that something
        # other than the runner could have written the output. Whatever it printed is program
        # output, not a test report, so the counts are discarded rather than recorded as if real:
        # leaving them in state would put fabricated numbers on a durable record. An authorized
        # expected-zero contract still resolves, because it asserts that nothing ran rather than
        # that anything passed.
        c["passed"] = c["failed"] = c["error"] = c["skipped"] = 0
        if expect_zero and exit_code in (0, None):
            return outcome(PASSED, "zero executed tests, explicitly authorized by the verification contract")
        return outcome(INCONCLUSIVE, "output is not attributable to a recognised test runner: not test evidence")

    executed = c["passed"] + c["failed"] + c["error"]
    if executed == 0:
        no_tests = exit_code == PYTEST_NO_TESTS_COLLECTED or bool(_NO_TESTS.search(output or "")) or collected == 0
        if expect_zero and exit_code in (0, PYTEST_NO_TESTS_COLLECTED, None):
            return outcome(PASSED, "zero executed tests, explicitly authorized by the verification contract")
        if c["skipped"]:
            return outcome(INCONCLUSIVE, f"{c['skipped']} test(s) collected but all skipped: no required test executed")
        if no_tests:
            return outcome(INCONCLUSIVE, "no tests were collected: execution required but not observed")
        if exit_code not in (0, None):
            return outcome(FAILED, f"command exited {exit_code} without executing any test")
        # Exit 0, nothing parsed, no explicit no-tests marker: the command succeeded at being a
        # command. That is not evidence about tests.
        return outcome(INCONCLUSIVE, "no structured test evidence: zero executed tests observed")

    if exit_code not in (0, None):
        return outcome(FAILED, f"{c['passed']} passed but the command exited {exit_code}")
    return outcome(PASSED, f"{c['passed']} passed, {c['skipped']} skipped")


# -- input version identity ------------------------------------------------------------


@dataclass
class InputVersionGuard:
    """Hashes the inputs a verification depends on, so a receipt can name what it tested.

    Verification that hashes only *after* the run cannot tell a coherent check from one whose
    inputs moved underneath it. Snapshotting before and comparing after closes that window as far
    as observation can: a file that changed mid-run is reported rather than silently attested.
    """

    paths: list[Path] = field(default_factory=list)

    def __init__(self, paths: Iterable[Path | str] = ()) -> None:
        self.paths = [Path(p) for p in paths]

    def snapshot(self) -> dict[str, str]:
        from cogos.verification.engine import file_sha256

        out: dict[str, str] = {}
        for p in self.paths:
            try:
                out[str(p)] = (file_sha256(p) or "") if p.is_file() else ""
            except OSError:
                out[str(p)] = ""
        return out

    def changed_since(self, before: Mapping[str, str]) -> list[str]:
        now = self.snapshot()
        return sorted(k for k in set(before) | set(now) if before.get(k, "") != now.get(k, ""))
