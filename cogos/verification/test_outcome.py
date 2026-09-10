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
_RUNNER_IN_COMMAND = re.compile(r"(?:^|[\s;&|/])(pytest|py\.test)\b|-m\s+pytest\b|-m\s+unittest\b|(?:^|[\s;&|/])nose2?\b")


def detect_framework(command: str) -> str:
    """Identify the runner from the **command**, never from the output.

    Output is written by the process under test. A program that prints "5 passed" would otherwise
    forge structured test evidence for itself — verified: a one-line script printing
    "Report: 5 passed, 0 failed" produced a PASSED verification with five fabricated tests. The
    command, by contrast, comes from the plan and has already been through the firewall.
    """
    m = _RUNNER_IN_COMMAND.search(command or "")
    if not m:
        return "unknown"
    text = m.group(0)
    if "unittest" in text:
        return "unittest"
    if "nose" in text:
        return "nose"
    return "pytest"


def parse_test_output(output: str) -> dict[str, int]:
    """Counts from a runner's summary line.

    Isolated here (rather than inline in the subprocess handler) so it can be tested against
    representative outputs, and anchored on the runner's ``N <kind> in <duration>s`` summary form
    so that a stray "5 passed" elsewhere in a log is not mistaken for a result line.
    """
    counts = {k: 0 for k in COUNT_KEYS}
    m = _PYTEST_SUMMARY.search(output or "")
    if not m:
        return counts
    for num, kind in _PYTEST_COUNTS.findall(m.group("summary")):
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
) -> TestRunOutcome:
    """Decide what a single test-command run proves.

    ``expect_zero`` must come from the verification contract as declared before execution.
    """
    c = {k: int((counts or {}).get(k, 0) or 0) for k in COUNT_KEYS}
    framework = detect_framework(command)
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
    if c["failed"] or c["error"]:
        return outcome(FAILED, f"{c['failed']} failed, {c['error']} error(s)")

    if framework == "unknown":
        # The command did not invoke a recognised test runner, so whatever it printed is program
        # output, not a test report. It may well have succeeded at being a command; that is not
        # evidence about tests. An authorized expected-zero contract still resolves, because it
        # asserts that nothing ran rather than that anything passed.
        if expect_zero and exit_code in (0, None):
            return outcome(PASSED, "zero executed tests, explicitly authorized by the verification contract")
        return outcome(INCONCLUSIVE, "command invoked no recognised test runner: not test evidence")

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
