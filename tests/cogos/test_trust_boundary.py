"""Trust-boundary regressions: every exploit that reached a false completion, and its controls.

Each attack here was reproduced end to end against the real pipeline at ``25ec140`` and reached
``mission_completion_check`` = PASSED over an implementation returning ``999.0``. They are kept as
tests because the refutation they carry is architectural, not incidental: **a report written by a
process that imports the code under test authenticates nothing.** A random filename, a nonce handed
to the child, a report hash, a trusted launcher and the exit code are each defeated, and a
``conftest.py`` hookwrapper defeats all of them at once by making the *genuine* runner write a
genuine, well-formed, freshly stamped report whose contents are false.

Every attack asserts the whole chain, not just the stage that happens to refuse it first: evidence
acceptance, verification status, criterion satisfaction, the completion gate, and the ground truth
of the implementation. A guard that refuses for an unrelated reason is not the guard under test, so
the controls at the bottom prove the pipeline still accepts honest work.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from cogos.config import GovernanceConfig
from cogos.governance.firewall import CapabilityFirewall
from cogos.schemas.common import PolicyDecision, VerificationStatus
from cogos.schemas.mission import Artifact, ArtifactExpectation, MissionState, SuccessCriterion, Task, TaskStatus
# Aliased: pytest would otherwise try to collect the schema class as a test class.
from cogos.schemas.mission import TestRecord as _TestRecord
from cogos.schemas.tools import ToolCall
from cogos.tools.fabric import ToolContext, build_default_fabric
from cogos.verification.attestation import EvidenceAuthority, at_least, authority_of
from cogos.verification.engine import MAX_BOUND_WORKSPACE_FILES, VerificationEngine, mission_completion_check

PASSED = VerificationStatus.PASSED
FAILED = VerificationStatus.FAILED
INCONCLUSIVE = VerificationStatus.INCONCLUSIVE

CORRECT = "def add_percent(value, percent):\n    return round(value * (1 + percent / 100.0), 2)\n"
#: Deliberately wrong, so accepting any forged evidence has a ground truth consequence that the
#: test can state rather than imply.
WRONG = "def add_percent(value, percent):\n    return 999.0\n"
REAL_TESTS = (
    "from calc import add_percent\n\n\n"
    "def test_positive():\n    assert add_percent(100, 10) == 110.0\n\n\n"
    "def test_zero():\n    assert add_percent(50, 0) == 50.0\n\n\n"
    "def test_negative():\n    assert add_percent(200, -50) == 100.0\n"
)
FORGED_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n<testsuites><testsuite name="pytest" errors="0" '
    'failures="0" skipped="0" tests="3" time="0.05">'
    '<testcase classname="test_calc" name="test_positive" time="0.01"/>'
    '<testcase classname="test_calc" name="test_zero" time="0.01"/>'
    '<testcase classname="test_calc" name="test_negative" time="0.01"/>'
    "</testsuite></testsuites>\n"
)


def _forging_module(payload: str = FORGED_XML, exit_zero: bool = True) -> str:
    """Workspace code that writes the report the engine asked for and leaves without running."""
    return textwrap.dedent(
        f"""
        import sys, os, atexit
        _P = [a.split('=', 1)[1] for a in sys.argv if a.startswith('--junitxml=')]
        def _w():
            for p in _P:
                open(p, 'w').write({payload!r})
            {'os._exit(0)' if exit_zero else 'pass'}
        atexit.register(_w)
        """
    )


def _workspace(tmp_path: Path, files: dict[str, str], impl: str = WRONG) -> Path:
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    (root / "calc.py").write_text(impl, encoding="utf-8")
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


class Outcome:
    """Every stage of the chain for one run, so a test can assert all of them."""

    def __init__(self, engine, state, record, verification, criterion, gate, root):
        self.engine, self.state, self.record = engine, state, record
        self.verification, self.criterion, self.gate, self.root = verification, criterion, gate, root

    # 1. evidence acceptance
    @property
    def report_accepted(self) -> bool:
        return bool(self.record and self.record.report_backed)

    # 5. ground truth, measured by importing the implementation in a clean interpreter
    def ground_truth(self) -> float:
        # `-P` keeps the workspace off the import path, so the module is named explicitly: this
        # reads the implementation as it is on disk, not through anything the workspace installs.
        program = f"import sys; sys.path.insert(0, {str(self.root)!r}); import calc; print(calc.add_percent(100, 10))"
        proc = subprocess.run(
            [sys.executable, "-P", "-c", program],
            cwd=str(self.root), capture_output=True, text=True, timeout=60,
            env={**os.environ, "PYTHONSAFEPATH": "1"},
        )
        return float(proc.stdout.strip() or "nan")


def run_chain(root: Path, command: str = "", *, trust_workspace_code: bool = True) -> Outcome:
    """Drive the real pipeline over a workspace and return every stage of the result.

    `trust_workspace_code` defaults to **True** here, which is the opposite of the production
    default and is deliberate. With it False the runtime refuses every in-process result outright,
    which would make each attack below pass for a reason that has nothing to do with the attack.
    Declaring the workspace non-adversarial is the hardest case for these controls: it is the
    configuration in which a forgery has the best chance, so it is the one they are tested in.
    The fail-closed default has its own test.
    """
    command = command or f"{sys.executable} -m pytest -q"
    firewall = CapabilityFirewall(GovernanceConfig(trust_workspace_code=trust_workspace_code), repo_root=root)
    fabric = build_default_fabric(firewall, ToolContext(repo_root=root))
    state = MissionState(objective="Build the feature described in REQUIREMENTS.md")
    criterion = SuccessCriterion(description="add_percent increases value by percent", verification_method="tests")
    state.success_criteria.append(criterion)
    task = Task(title="implement calc.add_percent", status=TaskStatus.DONE, addresses_criterion_ids=[criterion.id])
    state.tasks.append(task)
    engine = VerificationEngine(fabric, state)
    verification = engine.verify_code(commands=[command], cwd=str(root), task_id=task.id, criterion_ids=[criterion.id])
    engine.verify_criterion(criterion)
    return Outcome(engine, state, state.tests[-1] if state.tests else None, verification, criterion, mission_completion_check(state), root)


def assert_no_false_completion(out: Outcome, *, why: str) -> None:
    """The whole chain, in the order the evidence travels it."""
    assert out.ground_truth() == 999.0, "the fixture must be wrong, or the test proves nothing"
    assert out.verification.status != PASSED, f"{why}: verification passed"
    assert out.criterion.satisfied is False, f"{why}: criterion satisfied"
    assert out.gate.status != PASSED, f"{why}: mission completed — {out.gate.summary}"
    assert not at_least(out.record.authority if out.record else ""), f"{why}: record reached the behavioural authority floor"


# ======================================================================================
# 1. Verification evidence — the workspace cannot author its own result
# ======================================================================================


def test_a_workspace_pytest_module_cannot_impersonate_the_runner(tmp_path):
    """The original fatal finding: `pytest.py` in the workspace *is* the runner under
    `python -m pytest`, and it reads the report path out of its own argv."""
    root = _workspace(tmp_path, {"pytest.py": _forging_module(), "test_calc.py": REAL_TESTS})
    out = run_chain(root)
    assert_no_false_completion(out, why="a shadowing runner module")
    # The trusted path keeps the workspace off sys.path, so the real runner runs and reports the
    # real failures rather than the engine merely noticing something is wrong.
    assert out.record.counts["failed"] == 3, "the real runner must produce the real result"


def test_a_conftest_that_writes_the_report_and_forces_exit_zero_cannot_complete(tmp_path):
    root = _workspace(tmp_path, {"conftest.py": _forging_module(), "test_calc.py": REAL_TESTS})
    out = run_chain(root)
    assert_no_false_completion(out, why="a conftest that fabricates the report")


def test_a_conftest_that_flips_outcomes_cannot_complete(tmp_path):
    """The strongest forgery: no forged file at all. A hookwrapper turns every failure into a
    pass, and the *genuine* runner writes a genuine report whose contents are false — so report
    integrity, runner identity, freshness and file provenance all hold simultaneously."""
    root = _workspace(
        tmp_path,
        {
            "conftest.py": textwrap.dedent(
                """
                import pytest

                @pytest.hookimpl(hookwrapper=True)
                def pytest_runtest_makereport(item, call):
                    outcome = yield
                    report = outcome.get_result()
                    if report.outcome == 'failed':
                        report.outcome = 'passed'
                        report.longrepr = None
                """
            ),
            "test_calc.py": REAL_TESTS,
        },
    )
    out = run_chain(root)
    assert_no_false_completion(out, why="a conftest that flips outcomes")
    assert "does not depend on the code under test" in (out.record.attestation or ""), out.record.attestation


def test_subject_code_unregistering_the_report_plugin_cannot_complete(tmp_path):
    """In-process attack on the verifier's own machinery: the subject reaches into pytest's plugin
    manager, removes the plugin that writes the evidence, and writes the file itself."""
    root = _workspace(
        tmp_path,
        {
            "conftest.py": textwrap.dedent(
                f"""
                import sys, os, atexit

                def pytest_configure(config):
                    plugin = config.pluginmanager.get_plugin('junitxml')
                    if plugin is not None:
                        config.pluginmanager.unregister(plugin)

                _P = [a.split('=', 1)[1] for a in sys.argv if a.startswith('--junitxml=')]

                def _w():
                    for p in _P:
                        open(p, 'w').write({FORGED_XML!r})
                    os._exit(0)

                atexit.register(_w)
                """
            ),
            "test_calc.py": REAL_TESTS,
        },
    )
    out = run_chain(root)
    assert_no_false_completion(out, why="unregistering the report plugin")


def test_subject_code_monkeypatching_pytest_internals_cannot_complete(tmp_path):
    """The subject rewrites pytest's own report-writing class from inside the run, so the evidence
    is produced by the trusted runner's code path with the subject's contents."""
    root = _workspace(
        tmp_path,
        {
            "conftest.py": textwrap.dedent(
                f"""
                import _pytest.junitxml as jx

                _original = jx.LogXML.pytest_sessionfinish

                def _patched(self, *a, **k):
                    result = _original(self, *a, **k)
                    try:
                        open(str(self.logfile), 'w').write({FORGED_XML!r})
                    except Exception:
                        pass
                    return result

                jx.LogXML.pytest_sessionfinish = _patched

                def pytest_sessionfinish(session, exitstatus):
                    session.exitstatus = 0
                """
            ),
            "test_calc.py": REAL_TESTS,
        },
    )
    out = run_chain(root)
    assert_no_false_completion(out, why="monkeypatching pytest internals")


def test_noconftest_does_not_establish_the_boundary(tmp_path):
    """`--noconftest` removes one *file*, not the boundary problem.

    The first half measures the runner directly: with conftest loading disabled, an attack moved
    into the test module still produces a passing report from a failing suite. That is the reason
    the repair does not claim `--noconftest` as a control. The second half shows the pipeline
    refusing it anyway — by the differential control, not by the flag."""
    root = _workspace(tmp_path, {"test_calc.py": _forging_module() + "\n\ndef test_positive():\n    assert True\n"})
    report = tmp_path / "probe.xml"
    subprocess.run(
        [sys.executable, "-P", "-m", "pytest", "-q", "--noconftest", "-p", "no:cacheprovider", f"--junitxml={report}", str(root)],
        cwd=str(root), capture_output=True, text=True, timeout=180,
        env={**os.environ, "PYTHONSAFEPATH": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    assert report.is_file(), "the probe needs the runner to have produced a report"
    assert 'failures="0"' in report.read_text(encoding="utf-8"), "with --noconftest the subject still authored the evidence"

    out = run_chain(root)
    assert_no_false_completion(out, why="a test-module attack under --noconftest")


def test_a_hostile_test_module_cannot_complete(tmp_path):
    """No conftest, no shadow, no plugin, no config: the test module itself forges. There is no
    static property of the workspace that separates this from an honest suite."""
    root = _workspace(tmp_path, {"test_calc.py": _forging_module() + "\n\ndef test_positive():\n    assert True\n"})
    out = run_chain(root)
    assert_no_false_completion(out, why="a hostile test module")


def test_a_replayed_report_from_an_earlier_correct_run_cannot_complete(tmp_path):
    """A byte-identical replay of a real report produced from a correct implementation."""
    genuine = _workspace(tmp_path / "a", {"test_calc.py": REAL_TESTS}, impl=CORRECT)
    report = tmp_path / "genuine.xml"
    subprocess.run(
        [sys.executable, "-P", "-m", "pytest", "-q", f"--junitxml={report}", str(genuine)],
        cwd=str(genuine), capture_output=True, text=True, timeout=180,
        env={**os.environ, "PYTHONSAFEPATH": "1"},
    )
    assert report.is_file(), "the fixture needs a real report to replay"
    root = _workspace(tmp_path / "b", {"conftest.py": _forging_module(report.read_text(encoding="utf-8")), "test_calc.py": REAL_TESTS})
    out = run_chain(root)
    assert_no_false_completion(out, why="a replayed report")


def test_a_report_describing_a_different_suite_cannot_complete(tmp_path):
    other = (
        '<testsuites><testsuite name="pytest" errors="0" failures="0" skipped="0" tests="3">'
        '<testcase classname="tests.test_unrelated" name="test_import_works"/>'
        '<testcase classname="tests.test_unrelated" name="test_version_string"/>'
        '<testcase classname="tests.test_unrelated" name="test_noop"/>'
        "</testsuite></testsuites>\n"
    )
    root = _workspace(tmp_path, {"conftest.py": _forging_module(other), "test_calc.py": REAL_TESTS})
    out = run_chain(root)
    assert_no_false_completion(out, why="a report about a different suite")


def test_an_internally_inconsistent_report_cannot_complete(tmp_path):
    """A header declaring nine tests over one testcase element described a run that did not
    happen, and satisfied a criterion on the strength of the header alone."""
    inconsistent = (
        '<testsuites><testsuite name="pytest" errors="0" failures="0" skipped="0" tests="9">'
        '<testcase classname="test_calc" name="test_positive"/>'
        "</testsuite></testsuites>\n"
    )
    root = _workspace(tmp_path, {"conftest.py": _forging_module(inconsistent), "test_calc.py": REAL_TESTS})
    out = run_chain(root)
    assert_no_false_completion(out, why="an internally inconsistent report")


def test_workspace_pytest_ini_addopts_cannot_inject_a_plugin(tmp_path):
    """The trusted path supplies its own config file, so the workspace's ini is never read."""
    root = _workspace(
        tmp_path,
        {"pytest.ini": "[pytest]\naddopts = -p forge\n", "forge.py": _forging_module(), "test_calc.py": REAL_TESTS},
    )
    out = run_chain(root)
    assert_no_false_completion(out, why="addopts plugin injection")
    # Not merely "no completion": the injection has to have been ignored, which shows as the real
    # suite really running and really failing. Without this the differential control would stand in
    # for the guard under test and the test would pass with that guard removed.
    assert out.record.counts["failed"] == 3, out.record.outcome_reason


def test_pytest_addopts_in_the_ambient_environment_cannot_inject_a_plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p forge")
    root = _workspace(tmp_path, {"forge.py": _forging_module(), "test_calc.py": REAL_TESTS})
    out = run_chain(root)
    assert_no_false_completion(out, why="PYTEST_ADDOPTS injection")
    # Not merely "no completion": the injection has to have been ignored, which shows as the real
    # suite really running and really failing. Without this the differential control would stand in
    # for the guard under test and the test would pass with that guard removed.
    assert out.record.counts["failed"] == 3, out.record.outcome_reason


def test_a_workspace_sitecustomize_cannot_inject_itself(tmp_path):
    root = _workspace(tmp_path, {"sitecustomize.py": _forging_module(), "test_calc.py": REAL_TESTS})
    out = run_chain(root)
    assert_no_false_completion(out, why="sitecustomize injection")
    # Not merely "no completion": the injection has to have been ignored, which shows as the real
    # suite really running and really failing. Without this the differential control would stand in
    # for the guard under test and the test would pass with that guard removed.
    assert out.record.counts["failed"] == 3, out.record.outcome_reason


def test_a_pre_created_report_cannot_stand_in_for_a_run(tmp_path):
    """The evidence destination is a private directory created fresh per run, so there is nothing
    to pre-create; and a report older than the launch is refused outright."""
    root = _workspace(tmp_path, {"test_calc.py": REAL_TESTS})
    out = run_chain(root)
    # The real run fails honestly here (wrong implementation), which is the point: nothing the
    # workspace could have written beforehand is in play.
    assert out.verification.status == FAILED
    assert out.gate.status != PASSED


def test_a_report_that_contradicts_its_own_testcases_is_refused(tmp_path):
    """Isolated from the differential control: the header alone must not decide the counts."""
    from cogos.verification.attestation import parse_junit

    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuites><testsuite name="pytest" errors="0" failures="0" skipped="0" tests="9">'
        '<testcase classname="test_calc" name="test_positive"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    parsed = parse_junit(report)
    assert parsed is not None
    assert any("testcase element" in problem for problem in parsed.problems)

    report.write_text(FORGED_XML, encoding="utf-8")
    assert parse_junit(report).problems == ()


def test_a_report_about_another_suite_is_refused(tmp_path):
    """Isolated from the differential control: identities are checked back against the tree and
    against what was selected."""
    from cogos.verification.attestation import parse_junit, scope_problems

    (tmp_path / "test_calc.py").write_text(REAL_TESTS, encoding="utf-8")
    report = tmp_path / "junit.xml"
    report.write_text(FORGED_XML, encoding="utf-8")
    assert scope_problems(parse_junit(report), tmp_path, ["test_calc.py"]) == []

    report.write_text(
        '<testsuites><testsuite name="pytest" errors="0" failures="0" skipped="0" tests="1">'
        '<testcase classname="tests.test_unrelated" name="test_noop"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    problems = scope_problems(parse_junit(report), tmp_path, ["test_calc.py"])
    assert problems and "absent from the directory under test" in problems[0]


def test_a_report_older_than_the_run_is_refused(tmp_path):
    """Freshness, isolated from the differential control: the engine never compared the report
    against the moment it launched the run, so a backdated replay was read as this run's result."""
    import time

    from cogos.verification.attestation import read_report_once_settled

    report = tmp_path / "junit.xml"
    report.write_text(FORGED_XML, encoding="utf-8")
    stale = report.stat().st_mtime
    os.utime(report, (stale - 7 * 86400, stale - 7 * 86400))

    parsed, problems = read_report_once_settled(report, time.time())
    assert parsed is None and any("predates the run" in p for p in problems)

    os.utime(report, None)
    parsed, problems = read_report_once_settled(report, time.time() - 5)
    assert parsed is not None and problems == []


def test_unittest_has_no_trusted_path_and_is_never_authoritative(tmp_path):
    """`unittest` writes its own report to stderr, so a module printing a pytest-shaped summary
    owns stdout. There is no trusted path for it, so the run is diagnosis and closes nothing —
    and nothing falls back from the stronger verifier to this weaker evidence."""
    root = _workspace(
        tmp_path,
        {"test_calc.py": "import unittest\nprint('3 passed in 0.05s')\n\n\nclass T(unittest.TestCase):\n    def test_nothing(self):\n        pass\n"},
    )
    out = run_chain(root, f"{sys.executable} -m unittest discover -s . -p 'test_*.py'")
    assert_no_false_completion(out, why="a unittest stdout summary")
    assert out.verification.status == INCONCLUSIVE
    assert authority_of(out.record.authority) is EvidenceAuthority.UNTRUSTED_SELF_REPORT


def test_a_forged_report_does_not_switch_off_the_multi_summary_guard(tmp_path):
    """Making `report_backed` suppress the ambiguity guard meant fabricating a report was
    strictly *better* for an attacker than not fabricating one."""
    from cogos.verification.test_outcome import classify_test_run

    out = classify_test_run(
        exit_code=0,
        report_counts={"passed": 2, "failed": 0, "error": 0, "skipped": 0},
        output="3 passed in 0.12s\n...\n2 passed in 0.01s",
        command="python -m pytest -q",
    )
    assert out.status == INCONCLUSIVE and "more than one runner summary" in out.reason
    assert out.passed == 0


# ======================================================================================
# 2. Process execution and authorization — the action must not happen
# ======================================================================================


@pytest.fixture()
def sandbox(tmp_path):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    firewall = CapabilityFirewall(GovernanceConfig(), repo_root=repo)
    fabric = build_default_fabric(firewall, ToolContext(repo_root=repo))
    return repo, outside, fabric


def _shell(fabric, command, **kw):
    return fabric.execute(ToolCall(tool="shell", arguments={"command": command, **kw}))


@pytest.mark.parametrize(
    "name, template",
    [
        ("variable expansion", 'OUT={out}/pwned.txt; echo pwned > $OUT'),
        ("in-command chdir", 'cd {out} && echo pwned > pwned.txt'),
        ("backtick substitution", 'echo pwned > `echo {out}/pwned.txt`'),
        ("dollar substitution", 'echo pwned > $(echo {out}/pwned.txt)'),
        ("inline interpreter", 'python3 -c "open(\'{out}/pwned.txt\',\'w\').write(\'pwned\')"'),
    ],
)
def test_write_target_escapes_do_not_write_outside_the_writable_roots(sandbox, name, template):
    """All four documented escapes, plus `$( )`. Each was an authorization *bypass*: the literal
    form of the same write is denied, and these were allowed and landed on disk.

    The assertion is on the filesystem, not on a classifier label — a rule that classifies
    correctly and still lets the write happen is not a boundary."""
    repo, outside, fabric = sandbox
    res = _shell(fabric, template.format(out=outside))
    assert res.ok is False, f"{name} was authorized"
    assert not (outside / "pwned.txt").exists(), f"{name} wrote outside the writable roots"


def test_a_variable_expanded_deletion_does_not_delete(sandbox):
    """`rm -rf` is routed to human authorization; holding it in a shell variable executed it."""
    repo, outside, fabric = sandbox
    doomed = repo / "doomed"
    doomed.mkdir()
    (doomed / "keep.txt").write_text("keep", encoding="utf-8")

    control = _shell(fabric, "rm -rf doomed")
    assert control.verdict.decision is PolicyDecision.REQUIRE_HUMAN
    assert doomed.is_dir(), "the control must not delete either"

    evasion = _shell(fabric, 'X="rm -rf"; $X doomed')
    assert evasion.ok is False
    assert doomed.is_dir() and (doomed / "keep.txt").exists(), "the evasion deleted the fixture"


@pytest.mark.parametrize("command", ['V=env; cat .$V', "cat .e*", "cat .env"])
def test_credential_reads_do_not_return_the_secret(sandbox, command):
    """A glob or a variable named the same file the literal form is gated on, and the secret came
    back in tool output."""
    repo, outside, fabric = sandbox
    (repo / ".env").write_text("API_KEY=sk-fixture-secret-not-real\n", encoding="utf-8")
    res = _shell(fabric, command)
    assert "sk-fixture-secret-not-real" not in (res.output or ""), f"{command} disclosed the secret"
    assert res.ok is False


def test_a_git_alias_cannot_execute_an_arbitrary_program(sandbox):
    """`git` is a process spawner: an alias body beginning with `!` is a shell command."""
    repo, outside, fabric = sandbox
    res = fabric.execute(
        ToolCall(
            tool="git",
            # `touch`, not an interpreter: an inline-interpreter body would be refused by the
            # unanalysable-form rule and the git-specific rule would never be reached.
            arguments={"args": ["-c", f"alias.pwn=!touch {outside}/pwned.txt", "pwn"]},
        )
    )
    assert res.ok is False
    assert not (outside / "pwned.txt").exists(), "a git alias executed an arbitrary program"

    # The same alias writing *inside* the writable roots. The write-target rule has no objection to
    # it, so only the git-indirection rule can refuse it — and the marker file is the proof that it
    # did not run, rather than a classifier label standing in for the effect.
    inside = fabric.execute(ToolCall(tool="git", arguments={"args": ["-c", "alias.pwn=!touch marker.txt", "pwn"]}))
    assert inside.ok is False
    assert "execute an arbitrary program" in (inside.error or ""), inside.error
    assert not (repo / "marker.txt").exists(), "a git alias executed an arbitrary program inside the roots"


@pytest.mark.parametrize("substrate, tool, args", [
    ("shell", "shell", {"command": "echo hi"}),
    ("tests", "run_tests", {"command": "python -m pytest -q"}),
    ("git", "git", {"args": ["status"]}),
])
def test_disabling_shell_disables_every_process_spawning_substrate(tmp_path, substrate, tool, args):
    """`allow_shell=False` is a statement about running programs, not about one tool's name."""
    from cogos.schemas.tools import ToolSpec

    firewall = CapabilityFirewall(GovernanceConfig(allow_shell=False), tmp_path)
    verdict = firewall.check(ToolCall(tool=tool, arguments=args), ToolSpec(name=tool, description="", substrate=substrate))
    assert verdict.decision is PolicyDecision.DENY, f"{substrate} still spawns processes"


def test_the_test_runner_cannot_reach_a_denied_destination(sandbox):
    repo, outside, fabric = sandbox
    res = fabric.execute(ToolCall(tool="run_tests", arguments={"command": f"echo pwned > {outside}/pwned.txt"}))
    assert res.ok is False and not (outside / "pwned.txt").exists()


def test_ordinary_in_root_commands_are_still_authorized(sandbox):
    """The restriction must not take ordinary work with it."""
    repo, outside, fabric = sandbox
    assert _shell(fabric, "echo legit > inside.txt").ok is True
    assert (repo / "inside.txt").read_text(encoding="utf-8").strip() == "legit"
    assert _shell(fabric, "ls").ok is True
    assert _shell(fabric, "awk '{print $1}' inside.txt").ok is True, "a quoted $1 is not an expansion"


# ======================================================================================
# 3. Input and version binding
# ======================================================================================


def test_a_tree_too_large_to_bind_is_inconclusive_not_silently_unbound(tmp_path):
    """`_workspace_inputs` returned an empty list both for "nothing to bind" and "too many to
    bind", so a receipt over a large tree looked bound while binding nothing. This repository has
    thousands of eligible files, so every verification rooted at it fell into the second case."""
    root = _workspace(tmp_path, {"test_calc.py": REAL_TESTS}, impl=CORRECT)
    for i in range(MAX_BOUND_WORKSPACE_FILES + 5):
        (root / f"pad_{i}.txt").write_text("x", encoding="utf-8")
    out = run_chain(root)
    assert out.verification.status == INCONCLUSIVE
    assert any(c.name == "input_binding" for c in out.verification.checks)
    assert out.gate.status != PASSED


def test_an_implementation_outside_the_directory_under_test_cannot_be_attested(tmp_path):
    """Binding covers the tree the run happened in. An implementation one directory outside it is
    not bound — and is also not withheld by the control, so the pass is not attributable."""
    base = tmp_path / "base"
    inner = base / "tests"
    inner.mkdir(parents=True)
    (base / "impl.py").write_text("def solve():\n    return 42\n", encoding="utf-8")
    (inner / "test_solver.py").write_text(
        f"import sys\nsys.path.insert(0, {str(base)!r})\nfrom impl import solve\n\n\ndef test_solve():\n    assert solve() == 42\n",
        encoding="utf-8",
    )
    firewall = CapabilityFirewall(GovernanceConfig(), repo_root=base)
    fabric = build_default_fabric(firewall, ToolContext(repo_root=base))
    state = MissionState(objective="solve() returns 42")
    criterion = SuccessCriterion(description="solve returns 42", verification_method="tests")
    state.success_criteria.append(criterion)
    task = Task(title="implement", status=TaskStatus.DONE, addresses_criterion_ids=[criterion.id])
    state.tasks.append(task)
    engine = VerificationEngine(fabric, state)
    engine.verify_code(commands=[f"{sys.executable} -m pytest -q"], cwd=str(inner), task_id=task.id, criterion_ids=[criterion.id])
    engine.verify_criterion(criterion)

    (base / "impl.py").write_text("def solve():\n    return 999\n", encoding="utf-8")
    assert mission_completion_check(state).status != PASSED, "a swap outside the bound tree preserved the proof"


# ======================================================================================
# 4. Artifact semantics
# ======================================================================================


def test_a_placeholder_file_does_not_satisfy_a_criterion(tmp_path):
    """`verify_artifact` without a declared expectation establishes that a path resolved to a
    regular non-empty file whose bytes hash to H. A 20-byte `TODO: write this up` satisfied a
    criterion demanding a written analysis, indistinguishably from the finished deliverable."""
    path = tmp_path / "pricing.md"
    path.write_text("TODO: write this up\n", encoding="utf-8")
    state = MissionState(objective="Deliver the pricing analysis")
    artifact = Artifact(name="pricing analysis", path=str(path), summary="pricing analysis")
    state.artifacts.append(artifact)
    criterion = SuccessCriterion(description="pricing analysis delivered", verification_method="artifact")
    state.success_criteria.append(criterion)
    engine = VerificationEngine(None, state)
    engine.verify_artifact(artifact)

    assert artifact.verified is True, "the file is really there: integrity is not the thing in doubt"
    assert artifact.verified_scope == "existence"
    assert engine.verify_criterion(criterion).status == INCONCLUSIVE
    assert criterion.satisfied is False
    assert mission_completion_check(state).status != PASSED


def test_a_declared_content_expectation_is_what_makes_an_artifact_evidence(tmp_path):
    path = tmp_path / "pricing.md"
    path.write_text("TODO: write this up\n", encoding="utf-8")
    state = MissionState(objective="Deliver the pricing analysis")
    artifact = Artifact(
        name="pricing analysis",
        path=str(path),
        summary="pricing analysis",
        expectation=ArtifactExpectation(min_bytes=200, must_not_contain=["TODO"]),
    )
    state.artifacts.append(artifact)
    engine = VerificationEngine(None, state)
    assert engine.verify_artifact(artifact).status == FAILED, "the placeholder fails its own declared expectation"

    path.write_text("# Pricing analysis\n\n" + ("Competitor pricing averages 40 USD per seat. " * 12), encoding="utf-8")
    assert engine.verify_artifact(artifact).status == PASSED
    assert artifact.verified_scope == "existence+content"


def test_an_unrelated_artifact_does_not_ground_a_judgement(tmp_path):
    """Any verified file grounded a confident judgement about any criterion: meeting notes
    grounded a judgement about GDPR compliance."""
    from cogos.evaluation.support import Sandbox

    sb = Sandbox("tb-grounding")
    try:
        notes = sb.root / "meeting_notes.md"
        notes.write_text("Attendees: A, B. Next sync Thursday.\n", encoding="utf-8")
        state = MissionState(objective="Assess GDPR compliance")
        criterion = SuccessCriterion(description="the data export path is GDPR compliant", verification_method="prose only")
        state.success_criteria.append(criterion)
        artifact = Artifact(
            name="meeting notes",
            path=str(notes),
            summary="meeting notes",
            expectation=ArtifactExpectation(must_contain=["Attendees"]),
        )
        state.artifacts.append(artifact)
        VerificationEngine(sb.runtime.fabric, state).verify_artifact(artifact)
        assert artifact.verified_scope == "existence+content"

        assert sb.runtime.executive._judgment_grounding(state, criterion) == []
    finally:
        sb.cleanup()


# ======================================================================================
# 5. Gate applicability
# ======================================================================================


def test_a_check_that_skips_without_declaring_why_blocks_completion(tmp_path):
    """`_aggregate` ignored SKIPPED, so a check that turned itself off read as assent."""
    from cogos.schemas.verification import VerificationCheck
    from cogos.verification.engine import INAPPLICABLE, _gate_aggregate

    justified = [VerificationCheck(name="a", status=PASSED, detail="ok"), VerificationCheck(name="b", status=VerificationStatus.SKIPPED, detail=f"{INAPPLICABLE}: this mission declares no deliverable")]
    assert _gate_aggregate(justified) == PASSED

    silent = [VerificationCheck(name="a", status=PASSED, detail="ok"), VerificationCheck(name="b", status=VerificationStatus.SKIPPED, detail="nothing to check")]
    assert _gate_aggregate(silent) == INCONCLUSIVE


def test_required_deliverables_come_from_the_compiled_mission(tmp_path):
    """The one check that asks whether the deliverable was delivered was reachable only through an
    optional model-authored field that compilation never filled, so it always turned itself off."""
    from cogos.adapters.scripted import default_compilation
    from cogos.mission.compiler import derive_required_artifacts

    ctx = {"requirements_text": "Implement `calc.py` with `add_percent`. Add tests in `test_calc.py`.", "has_requirements": True}
    comp = default_compilation("Build the feature described in REQUIREMENTS.md", ctx)
    derived = derive_required_artifacts(comp, ctx)
    assert "calc.py" in derived and "test_calc.py" in derived
    assert "REQUIREMENTS.md" not in derived, "the specification is an input, not a deliverable"


# ======================================================================================
# 6. Authority levels, persistence and resume
# ======================================================================================


def test_a_record_with_no_authority_deserialises_to_the_weakest_level():
    """An absent field is indistinguishable from a stripped one and from a record produced under
    the rules this replaced, so the permissive reading reopens every hole at once."""
    assert authority_of("") is EvidenceAuthority.UNTRUSTED_SELF_REPORT
    assert authority_of(None) is EvidenceAuthority.UNTRUSTED_SELF_REPORT
    assert authority_of("something-else") is EvidenceAuthority.UNTRUSTED_SELF_REPORT
    assert at_least("") is False
    assert at_least(EvidenceAuthority.PROCESS_OBSERVATION.value) is False
    assert at_least(EvidenceAuthority.TRUSTED_HARNESS.value) is True
    assert at_least(EvidenceAuthority.INDEPENDENT_VALIDATION.value) is True


@pytest.mark.parametrize("authority", ["", EvidenceAuthority.UNTRUSTED_SELF_REPORT.value, EvidenceAuthority.PROCESS_OBSERVATION.value])
def test_evidence_below_the_floor_cannot_close_a_behavioural_criterion(authority):
    from cogos.ids import iso_now

    state = MissionState(objective="authority floor")
    criterion = SuccessCriterion(description="the suite passes", verification_method="pytest tests pass")
    state.success_criteria.append(criterion)
    state.tests.append(_TestRecord(name="pytest", status=PASSED, ran_at=iso_now(), criterion_ids=[criterion.id], counts={"passed": 3}, executed=3, authority=authority))
    engine = VerificationEngine(None, state)

    assert engine.verify_criterion(criterion).status != PASSED
    assert criterion.satisfied is False
    assert mission_completion_check(state).status != PASSED


def test_authority_survives_persistence_and_resume(tmp_path):
    """The level has to be on the durable record, or resume restores evidence without its limits."""
    from cogos.ids import iso_now

    state = MissionState(objective="persisted authority")
    criterion = SuccessCriterion(description="the suite passes", verification_method="pytest tests pass")
    state.success_criteria.append(criterion)
    state.tests.append(
        _TestRecord(name="pytest", status=PASSED, ran_at=iso_now(), criterion_ids=[criterion.id], counts={"passed": 3}, executed=3, authority=EvidenceAuthority.TRUSTED_HARNESS.value)
    )
    restored = MissionState.model_validate_json(state.model_dump_json())
    assert restored.tests[0].authority == EvidenceAuthority.TRUSTED_HARNESS.value
    assert at_least(restored.tests[0].authority)

    # A snapshot written before the field existed loads, and lands at the weakest level.
    raw = state.model_dump(mode="json")
    raw["tests"][0].pop("authority")
    legacy = MissionState.model_validate(raw)
    assert legacy.tests[0].authority == ""
    assert at_least(legacy.tests[0].authority) is False


# ======================================================================================
# Legitimate controls — fail-closed must not destroy supported functionality
# ======================================================================================


def test_an_honest_workspace_with_a_correct_implementation_completes(tmp_path):
    root = _workspace(tmp_path, {"test_calc.py": REAL_TESTS}, impl=CORRECT)
    out = run_chain(root)
    assert out.ground_truth() == 110.0
    assert out.verification.status == PASSED, out.verification.summary
    assert out.criterion.satisfied is True
    assert out.gate.status == PASSED, out.gate.summary
    assert authority_of(out.record.authority) is EvidenceAuthority.TRUSTED_HARNESS
    assert at_least(out.record.authority)


def test_the_default_refuses_in_process_evidence_outright(tmp_path):
    """Fail closed. The subject's code runs inside the process that reports on it, and this
    runtime has no boundary that establishes it did not interfere — so by default an in-process
    result is capped below the behavioural floor and cannot close a criterion, however honest the
    workspace and however green the run.

    Declaring a workspace non-adversarial is a human authorization decision about that workspace,
    not a tuning knob, and it never disables the harness or the control: it only lets their result
    carry authority."""
    root = _workspace(tmp_path, {"test_calc.py": REAL_TESTS}, impl=CORRECT)
    out = run_chain(root, trust_workspace_code=False)

    assert out.ground_truth() == 110.0, "the implementation really is correct"
    assert out.verification.status == INCONCLUSIVE
    assert "same-process verification" in out.record.outcome_reason
    assert authority_of(out.record.authority) is EvidenceAuthority.PROCESS_OBSERVATION
    assert out.criterion.satisfied is False
    assert out.gate.status != PASSED, "correct work must not complete on evidence the runtime cannot stand behind"


def test_an_honest_workspace_with_a_wrong_implementation_fails_honestly(tmp_path):
    """The gate must refuse for the right reason: real tests really failed."""
    root = _workspace(tmp_path, {"test_calc.py": REAL_TESTS}, impl=WRONG)
    out = run_chain(root)
    assert out.verification.status == FAILED
    assert out.record.counts["failed"] == 3
    assert out.gate.status != PASSED


def test_a_correct_implementation_swapped_after_verification_blocks_completion(tmp_path):
    root = _workspace(tmp_path, {"test_calc.py": REAL_TESTS}, impl=CORRECT)
    out = run_chain(root)
    assert out.gate.status == PASSED

    (root / "calc.py").write_text(WRONG, encoding="utf-8")
    assert mission_completion_check(out.state).status != PASSED, "a post-verification swap preserved the proof"
