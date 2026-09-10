"""Incident regressions for the evidence pipeline exposed by Live Run #2.

The pipeline under test is::

    AUTHORIZED ACTION -> OBSERVABLE RESULT -> STRUCTURED EVIDENCE
    -> ARTIFACT/VERSION IDENTITY -> CRITERION-SPECIFIC VERIFICATION -> RECEIPT -> COMPLETION GATE

Three defects broke it, each confirmed against the live mission database:

F1  A VERIFY/FALSIFY task carrying an authorized tool plan (``parameters {"tool": ..., "arguments": ...}``)
    never executed it: ``Executive._verify`` recognised only ``commands``/``test_command``/``verify_commands``,
    so the plan was ignored, the task produced no result, and executive judgement failed it for
    "RESULT SUMMARY is empty". Live cycles 3, 4 and 7.

F2  ``VerificationEngine.verify_code`` set PASSED from the process exit code alone. A command that
    collected zero tests and exited 0 was recorded as a passing test verification. Live receipt
    ``ver_1m26hx9rc493b844d``: "1/1 commands passed ...; tests: 0 passed, 0 failed, 0 errors".

F3  Nothing registered a file the executive wrote itself. ``state.artifacts.append`` existed at exactly
    one site, inside the specialist-report loop, so ``mission.artifacts`` stayed empty for the whole run
    and the gate's ``required_artifacts`` check could never pass.

Every test here asserts the *correct* behaviour, so on the pre-fix implementation it fails for the
defect's own reason rather than on an import.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cogos.beliefs import BeliefGraph
from cogos.config import GovernanceConfig
from cogos.evaluation.support import Sandbox
from cogos.executive.controller import Assessment
from cogos.governance.firewall import CapabilityFirewall
from cogos.observability.ledger import ResourceLedger
from cogos.planner import Planner
from cogos.schemas.cognition import OperationKind, StepDecision
from cogos.schemas.common import VerificationStatus
from cogos.schemas.mission import Artifact, MissionState, SuccessCriterion, Task, TaskStatus
from cogos.tools import build_default_fabric
from cogos.tools.fabric import ToolContext
from cogos.verification import VerificationEngine, mission_completion_check

PASSED = VerificationStatus.PASSED
FAILED = VerificationStatus.FAILED
INCONCLUSIVE = VerificationStatus.INCONCLUSIVE
# The enum has no ERROR member: a collection error is FAILED under existing semantics.

CALC = '''"""Small calculation helpers."""


def add_percent(value: float, percent: float) -> float:
    """Return ``value`` increased by ``percent`` percent, rounded to 2 decimals."""
    return round(value * (1 + percent / 100), 2)
'''

TEST_CALC = '''from calc import add_percent


def test_positive():
    assert add_percent(100, 10) == 110.0


def test_zero():
    assert add_percent(100, 0) == 100.0


def test_negative():
    assert add_percent(200, -25) == 150.0
'''

BROKEN_CALC = '''def add_percent(value: float, percent: float) -> float:
    return round(value * (1 + percent), 2)  # BUG: percent not divided by 100
'''


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _fabric(root: Path):
    return build_default_fabric(CapabilityFirewall(GovernanceConfig(), root), ToolContext(root))


def _perform(sb: Sandbox, state: MissionState, decision: StepDecision, task: Task | None):
    """Drive one operation through the real dispatch, exactly as a cycle would."""
    ex = sb.runtime.executive
    return ex._perform(state, decision, task, BeliefGraph(state), Planner(state), ResourceLedger(state.usage), Assessment())


def _verify_task(task_id: str) -> StepDecision:
    return StepDecision(operation=OperationKind.VERIFY, task_id=task_id, rationale="verify the deliverable", confidence=0.8)


def _write(root: Path, name: str, body: str) -> Path:
    p = root / name
    p.write_text(body, encoding="utf-8")
    return p


# ======================================================================================
# F1 — task purpose is not an execution mechanism
# ======================================================================================


def test_f1_tool_backed_verify_executes_its_plan_and_retains_the_result():
    """The live failure: a VERIFY task carrying {"tool": "shell", "arguments": {...}} never ran.

    Live cycles 3/4/7 each produced "RESULT SUMMARY is empty — no artifact to evaluate."
    """
    sb = Sandbox("f1-verify-executes")
    try:
        state = sb.runtime.new_mission("check the workspace", context=sb.context(has_requirements=False))
        _write(sb.root, "marker.txt", "COGOS_MARKER_OK\n")
        task = Task(
            title="Read the marker back",
            status=TaskStatus.ACTIVE,
            operation_hint="verify",
            parameters={"tool": "shell", "arguments": {"command": "cat marker.txt", "cwd": str(sb.root)}},
        )
        state.tasks.append(task)

        out = _perform(sb, state, _verify_task(task.id), task)

        assert out.tool_results, "a tool-backed VERIFY task must execute its plan through the ordinary tool path"
        res = out.tool_results[0]
        assert res.tool == "shell" and res.ok, f"the shell call should have run: {res.error!r}"
        assert "COGOS_MARKER_OK" in res.output, "the actual observation must be retained"
        assert out.verification is not None, "verification must be produced from the observation"
        assert "COGOS_MARKER_OK" in json.dumps(out.verification.model_dump(mode="json")), (
            "the verifier must receive the actual tool observation, not an empty result"
        )
    finally:
        sb.cleanup()


def test_f1_tool_backed_falsify_executes_its_plan():
    """Live cycle 8: a task pinned to execute_code was routed as falsify and never ran its command."""
    sb = Sandbox("f1-falsify-executes")
    try:
        state = sb.runtime.new_mission("falsify a belief", context=sb.context(has_requirements=False))
        _write(sb.root, "probe.txt", "FALSIFY_PROBE\n")
        task = Task(
            title="Deterministically falsify via a file read",
            status=TaskStatus.ACTIVE,
            operation_hint="falsify",
            parameters={"tool": "shell", "arguments": {"command": "cat probe.txt", "cwd": str(sb.root)}},
        )
        state.tasks.append(task)

        out = _perform(
            sb,
            state,
            StepDecision(operation=OperationKind.FALSIFY, task_id=task.id, rationale="falsify deterministically", confidence=0.7),
            task,
        )

        assert out.tool_results, "a tool-backed FALSIFY task must execute its plan"
        assert "FALSIFY_PROBE" in out.tool_results[0].output
        assert state.usage.subagents_spawned == 0, "a deterministic falsification must not burn a subagent"
    finally:
        sb.cleanup()


def test_f1_verify_purpose_grants_no_extra_authority():
    """Purpose is not authorization: a denied tool stays denied under VERIFY."""
    sb = Sandbox("f1-no-escalation", governance=GovernanceConfig(denied_action_classes=["destructive"]))
    try:
        state = sb.runtime.new_mission("try to escalate", context=sb.context(has_requirements=False))
        victim = _write(sb.root, "keep.txt", "precious\n")
        outside = Path("/etc/cogos-should-never-exist.txt")
        task = Task(
            title="Write outside the workspace under a verify purpose",
            status=TaskStatus.ACTIVE,
            operation_hint="verify",
            parameters={"tool": "write_file", "arguments": {"path": str(outside), "content": "pwned"}},
        )
        state.tasks.append(task)

        out = _perform(sb, state, _verify_task(task.id), task)

        assert not outside.exists(), "VERIFY must not become a way to write outside the workspace"
        if out.tool_results:
            res = out.tool_results[0]
            assert not res.ok, "an out-of-workspace write must not succeed under a verify purpose"
            assert res.error_kind in ("denied", "requires_human", "unavailable"), res.error_kind
        assert victim.read_text(encoding="utf-8") == "precious\n"
    finally:
        sb.cleanup()


def test_f1_criterion_prose_is_never_executed_as_a_command():
    """A criterion's verification_method is prose. It must never reach a shell."""
    sb = Sandbox("f1-no-prose-execution")
    try:
        state = sb.runtime.new_mission("prose safety", context=sb.context(has_requirements=False))
        sentinel = sb.root / "PROSE_EXECUTED"
        state.success_criteria.append(
            SuccessCriterion(
                description="the deliverable exists",
                verification_method=f"run `touch {sentinel}` to confirm; inspect_files on the result",
            )
        )
        task = Task(title="Verify the criterion", status=TaskStatus.ACTIVE, operation_hint="verify")
        state.tasks.append(task)

        _perform(sb, state, _verify_task(task.id), task)

        assert not sentinel.exists(), "criterion prose must never be converted into shell execution"
    finally:
        sb.cleanup()


def test_f1_a_denied_plan_keeps_its_real_status_and_does_not_read_as_success():
    sb = Sandbox("f1-denied-status", governance=GovernanceConfig(denied_action_classes=["destructive", "consequential_shared"]))
    try:
        state = sb.runtime.new_mission("denied stays denied", context=sb.context(has_requirements=False))
        task = Task(
            title="Denied write",
            status=TaskStatus.ACTIVE,
            operation_hint="verify",
            parameters={"tool": "write_file", "arguments": {"path": "/etc/nope.txt", "content": "x"}},
        )
        state.tasks.append(task)

        out = _perform(sb, state, _verify_task(task.id), task)

        if out.verification is not None:
            assert out.verification.status != PASSED, "a denied action must never verify as passed"
    finally:
        sb.cleanup()


# ======================================================================================
# F2 — zero-test and test-scope semantics
# ======================================================================================


def test_f2_real_tests_that_execute_and_pass_are_eligible_for_pass(tmp_path):
    _write(tmp_path, "calc.py", CALC)
    _write(tmp_path, "test_calc.py", TEST_CALC)
    state = MissionState(objective="run real tests")
    engine = VerificationEngine(_fabric(tmp_path), state)

    res = engine.verify_code([f"{sys.executable} -m pytest -q"], cwd=str(tmp_path))

    assert res.status == PASSED, res.summary
    assert state.tests and state.tests[-1].status == PASSED


def test_f2_test_failures_fail(tmp_path):
    _write(tmp_path, "calc.py", BROKEN_CALC)
    _write(tmp_path, "test_calc.py", TEST_CALC)
    state = MissionState(objective="failing tests")
    engine = VerificationEngine(_fabric(tmp_path), state)

    res = engine.verify_code([f"{sys.executable} -m pytest -q"], cwd=str(tmp_path))

    assert res.status == FAILED, res.summary


def test_f2_collection_error_is_not_a_pass(tmp_path):
    _write(tmp_path, "test_broken.py", "import nonexistent_module_xyz\n\n\ndef test_x():\n    assert True\n")
    state = MissionState(objective="collection error")
    engine = VerificationEngine(_fabric(tmp_path), state)

    res = engine.verify_code([f"{sys.executable} -m pytest -q"], cwd=str(tmp_path))

    assert res.status == FAILED, f"a collection error must not pass: {res.summary}"


def test_f2_exit_zero_with_zero_collection_is_inconclusive_never_pass(tmp_path):
    """THE live defect: `python -c "..."` exits 0, runs no tests, and was recorded PASSED."""
    state = MissionState(objective="zero collection")
    engine = VerificationEngine(_fabric(tmp_path), state)

    res = engine.verify_code([f'{sys.executable} -c "print(42)"'], cwd=str(tmp_path))

    assert res.status != PASSED, (
        f"a command that executed zero tests must never be a passing test verification: {res.summary}"
    )
    assert res.status == INCONCLUSIVE, res.summary
    assert state.tests and state.tests[-1].status != PASSED, "the test ledger must not record a passing run"


def test_f2_pytest_no_tests_collected_is_inconclusive(tmp_path):
    """pytest exits 5 when it collects nothing. That is 'unproven', not 'failed' and not 'passed'."""
    (tmp_path / "nothing_here.txt").write_text("no tests", encoding="utf-8")
    state = MissionState(objective="pytest exit 5")
    engine = VerificationEngine(_fabric(tmp_path), state)

    res = engine.verify_code([f"{sys.executable} -m pytest -q"], cwd=str(tmp_path))

    assert res.status == INCONCLUSIVE, f"pytest exit 5 (no tests collected) must be inconclusive: {res.summary}"


def test_f2_all_skipped_is_not_proof_that_required_tests_ran(tmp_path):
    _write(tmp_path, "test_skipped.py", "import pytest\n\n\n@pytest.mark.skip(reason='not today')\ndef test_x():\n    assert True\n")
    state = MissionState(objective="all skipped")
    engine = VerificationEngine(_fabric(tmp_path), state)

    res = engine.verify_code([f"{sys.executable} -m pytest -q"], cwd=str(tmp_path))

    assert res.status != PASSED, f"an entirely skipped suite does not prove the required tests executed: {res.summary}"


def test_f2_explicitly_preauthorized_expected_zero_is_honoured(tmp_path):
    """The exception must be declared in the contract *before* execution, never invented after."""
    state = MissionState(objective="expected zero")
    engine = VerificationEngine(_fabric(tmp_path), state)

    res = engine.verify_code([f'{sys.executable} -c "print(1)"'], cwd=str(tmp_path), expect_zero_tests=True)

    assert res.status == PASSED, f"a pre-authorized expected-zero run may pass: {res.summary}"


def test_f2_an_unrelated_passing_suite_cannot_prove_a_differently_scoped_criterion(tmp_path):
    """A passing test record must be bound to the criterion it is offered as proof of."""
    state = MissionState(objective="scope binding")
    engine = VerificationEngine(_fabric(tmp_path), state)
    target = SuccessCriterion(description="the payments module rounds correctly", verification_method="pytest tests for the payments module pass")
    state.success_criteria.append(target)

    _write(tmp_path, "test_unrelated.py", "def test_unrelated():\n    assert True\n")
    unrelated_task = Task(title="run an unrelated suite", status=TaskStatus.DONE)
    state.tasks.append(unrelated_task)
    engine.verify_code([f"{sys.executable} -m pytest -q"], cwd=str(tmp_path), task_id=unrelated_task.id)

    res = engine.verify_criterion(target)

    assert res.status != PASSED, "an unrelated passing suite must not satisfy a differently scoped criterion"
    assert not target.satisfied


def test_f2_missing_structured_evidence_cannot_silently_pass(tmp_path):
    """No counts, no exit code, nothing to read: that is inconclusive, not success."""
    from cogos.verification.test_outcome import classify_test_run

    outcome = classify_test_run(exit_code=None, counts={}, output="", command="mystery")

    assert outcome.status != PASSED, "ambiguous execution evidence must never become PASS"
    assert outcome.status == INCONCLUSIVE


# ======================================================================================
# F3 — artifact candidates, provenance and version identity
# ======================================================================================


def test_f3_an_authorized_executive_write_registers_an_unverified_candidate():
    """The live gap: calc.py and test_calc.py were written and correct, and the ledger stayed empty."""
    sb = Sandbox("f3-registers-candidate")
    try:
        state = sb.runtime.new_mission("write a deliverable", context=sb.context(has_requirements=False))
        task = Task(title="Write calc.py", status=TaskStatus.ACTIVE, operation_hint="execute_code",
                    parameters={"tool": "write_file", "arguments": {"path": "calc.py", "content": CALC}})
        state.tasks.append(task)

        _perform(sb, state, StepDecision(operation=OperationKind.EXECUTE_CODE, task_id=task.id, rationale="write it", confidence=0.9), task)

        assert state.artifacts, "a successful authorized mission write must register an artifact candidate"
        art = next(a for a in state.artifacts if a.path and a.path.endswith("calc.py"))
        assert art.verified is False, "registration must never imply verification"
        assert art.content_hash, "a candidate must carry immutable version identity"
        assert art.produced_by_task_id == task.id, "provenance must identify the producing task"
    finally:
        sb.cleanup()


def test_f3_registration_alone_leaves_every_criterion_unsatisfied():
    sb = Sandbox("f3-registration-is-not-proof")
    try:
        state = sb.runtime.new_mission("registration is not proof", context=sb.context(has_requirements=False))
        crit = SuccessCriterion(description="calc.py exists and defines add_percent", verification_method="inspect the artifact")
        state.success_criteria.append(crit)
        task = Task(title="Write calc.py", status=TaskStatus.ACTIVE, operation_hint="execute_code",
                    parameters={"tool": "write_file", "arguments": {"path": "calc.py", "content": CALC}})
        state.tasks.append(task)

        _perform(sb, state, StepDecision(operation=OperationKind.EXECUTE_CODE, task_id=task.id, rationale="write it", confidence=0.9), task)

        assert state.artifacts, "precondition: the candidate registered"
        assert not any(c.satisfied for c in state.success_criteria), "registration must not satisfy a criterion"
        gate = mission_completion_check(state)
        assert gate.status != PASSED, "the gate must still refuse: nothing has been verified"
    finally:
        sb.cleanup()


def test_f3_a_failed_write_never_registers_a_candidate():
    sb = Sandbox("f3-failed-write", governance=GovernanceConfig(denied_action_classes=["consequential_shared", "destructive"]))
    try:
        state = sb.runtime.new_mission("failed write", context=sb.context(has_requirements=False))
        task = Task(title="Write outside", status=TaskStatus.ACTIVE, operation_hint="execute_code",
                    parameters={"tool": "write_file", "arguments": {"path": "/etc/cogos-nope.txt", "content": "x"}})
        state.tasks.append(task)

        _perform(sb, state, StepDecision(operation=OperationKind.EXECUTE_CODE, task_id=task.id, rationale="write", confidence=0.5), task)

        assert not state.artifacts, "a denied or failed write must never appear as a successful creation"
    finally:
        sb.cleanup()


def test_f3_a_pre_existing_file_is_not_misrepresented_as_mission_created():
    sb = Sandbox("f3-pre-existing")
    try:
        pre = _write(sb.root, "already_here.py", "# written before the mission\n")
        state = sb.runtime.new_mission("pre-existing files", context=sb.context(has_requirements=False))
        task = Task(title="Write something else", status=TaskStatus.ACTIVE, operation_hint="execute_code",
                    parameters={"tool": "write_file", "arguments": {"path": "new.py", "content": "# new\n"}})
        state.tasks.append(task)

        _perform(sb, state, StepDecision(operation=OperationKind.EXECUTE_CODE, task_id=task.id, rationale="write", confidence=0.9), task)

        paths = [a.path for a in state.artifacts if a.path]
        assert not any(p.endswith("already_here.py") for p in paths), (
            "a file the mission never wrote must not be laundered into the artifact ledger"
        )
        assert pre.exists()
    finally:
        sb.cleanup()


def test_f3_rewriting_a_file_creates_a_new_version_and_drops_verified_status():
    sb = Sandbox("f3-new-version")
    try:
        state = sb.runtime.new_mission("versions", context=sb.context(has_requirements=False))
        task = Task(title="Write v1", status=TaskStatus.ACTIVE, operation_hint="execute_code",
                    parameters={"tool": "write_file", "arguments": {"path": "calc.py", "content": CALC}})
        state.tasks.append(task)
        _perform(sb, state, StepDecision(operation=OperationKind.EXECUTE_CODE, task_id=task.id, rationale="v1", confidence=0.9), task)
        art = next(a for a in state.artifacts if a.path and a.path.endswith("calc.py"))
        first_hash = art.content_hash
        art.verified = True
        art.verified_hash = first_hash

        task2 = Task(title="Write v2", status=TaskStatus.ACTIVE, operation_hint="execute_code",
                     parameters={"tool": "write_file", "arguments": {"path": "calc.py", "content": BROKEN_CALC}})
        state.tasks.append(task2)
        _perform(sb, state, StepDecision(operation=OperationKind.EXECUTE_CODE, task_id=task2.id, rationale="v2", confidence=0.9), task2)

        art2 = next(a for a in state.artifacts if a.path and a.path.endswith("calc.py"))
        assert art2.content_hash != first_hash, "a rewrite must produce a new version identity"
        assert art2.verified is False, "a changed artifact must lose verified status"
        assert first_hash in json.dumps(art2.model_dump(mode="json")), "history must be retained, not erased"
    finally:
        sb.cleanup()


def test_f3_criterion_specific_verification_of_a_candidate_produces_a_genuine_receipt(tmp_path):
    state = MissionState(objective="genuine receipt")
    engine = VerificationEngine(_fabric(tmp_path), state)
    p = _write(tmp_path, "calc.py", CALC)
    art = Artifact(name="calc.py", kind="code", path=str(p))
    state.artifacts.append(art)

    res = engine.verify_artifact(art)

    assert res.status == PASSED
    assert art.verified and art.verified_hash
    assert res.is_evidence_for("artifact", art.id), "the receipt must bind to the artifact it checked"


def test_f3_wrong_content_and_empty_file_do_not_verify(tmp_path):
    state = MissionState(objective="content matters")
    engine = VerificationEngine(_fabric(tmp_path), state)
    empty = _write(tmp_path, "empty.py", "")
    art = Artifact(name="empty.py", kind="code", path=str(empty))
    state.artifacts.append(art)

    res = engine.verify_artifact(art)

    assert res.status == FAILED, "an empty file must not verify when content is required"
    assert not art.verified


def test_f3_an_outside_workspace_path_is_not_registered():
    sb = Sandbox("f3-outside-workspace")
    try:
        state = sb.runtime.new_mission("boundaries", context=sb.context(has_requirements=False))
        task = Task(title="Write outside", status=TaskStatus.ACTIVE, operation_hint="execute_code",
                    parameters={"tool": "write_file", "arguments": {"path": "/tmp/cogos-outside-registry.txt", "content": "x"}})
        state.tasks.append(task)

        _perform(sb, state, StepDecision(operation=OperationKind.EXECUTE_CODE, task_id=task.id, rationale="write", confidence=0.5), task)

        outside = [a for a in state.artifacts if a.path and not str(Path(a.path)).startswith(str(sb.root))]
        assert not outside, f"a write outside the workspace must not enter the ledger: {outside}"
    finally:
        sb.cleanup()


# ======================================================================================
# Evidence identity and mutation safety (section 7)
# ======================================================================================


def test_evidence_identity_a_receipt_records_the_input_versions_it_was_produced_against(tmp_path):
    _write(tmp_path, "calc.py", CALC)
    _write(tmp_path, "test_calc.py", TEST_CALC)
    state = MissionState(objective="identity")
    engine = VerificationEngine(_fabric(tmp_path), state)
    art = Artifact(name="calc.py", kind="code", path=str(tmp_path / "calc.py"))
    state.artifacts.append(art)
    engine.verify_artifact(art)

    res = engine.verify_code([f"{sys.executable} -m pytest -q"], cwd=str(tmp_path))

    assert res.status == PASSED
    assert res.input_versions, "a code receipt must record which input versions it ran against"
    hashes = {iv.content_hash for iv in res.input_versions}
    assert art.content_hash in hashes, "the verified artifact's version must be bound into the receipt"


def test_evidence_identity_a_receipt_for_version_a_does_not_prove_version_b(tmp_path):
    p = _write(tmp_path, "calc.py", CALC)
    _write(tmp_path, "test_calc.py", TEST_CALC)
    state = MissionState(objective="stale receipt")
    engine = VerificationEngine(_fabric(tmp_path), state)
    art = Artifact(name="calc.py", kind="code", path=str(p))
    state.artifacts.append(art)
    engine.verify_artifact(art)
    crit = SuccessCriterion(description="calc rounds correctly", verification_method="pytest tests pass")
    state.success_criteria.append(crit)
    receipt = engine.verify_code([f"{sys.executable} -m pytest -q"], cwd=str(tmp_path))
    assert receipt.status == PASSED

    # The implementation is swapped after verification and before completion.
    p.write_text(BROKEN_CALC, encoding="utf-8")

    gate = mission_completion_check(state)

    assert gate.status != PASSED, "a receipt must not survive a change to the inputs it was produced against"
    assert "calc.py" in gate.summary or "version" in gate.summary.lower() or "artifact" in gate.summary.lower()


def test_evidence_identity_mutation_during_verification_is_detected(tmp_path):
    """A file that changes while the check runs was never coherently verified."""
    from cogos.verification.test_outcome import InputVersionGuard

    p = _write(tmp_path, "calc.py", CALC)
    guard = InputVersionGuard([p])
    before = guard.snapshot()
    p.write_text(BROKEN_CALC, encoding="utf-8")

    changed = guard.changed_since(before)

    assert changed, "a mid-verification mutation must be detectable"


def test_evidence_identity_a_stale_hash_invalidates_current_proof(tmp_path):
    state = MissionState(objective="stale hash")
    p = _write(tmp_path, "calc.py", CALC)
    art = Artifact(name="calc.py", kind="code", path=str(p), verified=True, verified_hash="0" * 64, content_hash="0" * 64)
    state.artifacts.append(art)
    state.success_criteria.append(SuccessCriterion(description="calc.py exists", verification_method="artifact check"))

    gate = mission_completion_check(state)

    assert gate.status != PASSED, "an artifact whose bytes no longer match its verified hash cannot support completion"


def test_evidence_identity_traversal_is_machine_readable_in_both_directions(tmp_path):
    """criterion -> receipt -> verification -> evidence -> action, and back, by id alone."""
    _write(tmp_path, "calc.py", CALC)
    _write(tmp_path, "test_calc.py", TEST_CALC)
    state = MissionState(objective="traversal")
    engine = VerificationEngine(_fabric(tmp_path), state)
    crit = SuccessCriterion(description="the suite passes", verification_method="pytest tests pass")
    state.success_criteria.append(crit)
    task = Task(title="run tests", status=TaskStatus.DONE, addresses_criterion_ids=[crit.id])
    state.tasks.append(task)

    code = engine.verify_code([f"{sys.executable} -m pytest -q"], cwd=str(tmp_path), task_id=task.id)
    assert code.status == PASSED
    res = engine.verify_criterion(crit)
    assert res.status == PASSED, res.summary

    # forward: criterion -> receipt
    receipts = state.passing_verifications(crit.verification_ids, target_type="criterion", target_id=crit.id)
    assert receipts, "criterion must resolve to a bound passing receipt by id"
    # backward: test record -> the criterion it was offered for
    rec = state.tests[-1]
    assert crit.id in rec.criterion_ids, "a test record must name every criterion it is evidence for"


# ======================================================================================
# End-to-end incident regression + negative control
# ======================================================================================


def test_end_to_end_incident_pipeline_completes_only_with_real_evidence():
    """Write -> register -> VERIFY through the runtime -> real tests -> receipts -> gate."""
    sb = Sandbox("e2e-incident")
    try:
        state = sb.runtime.new_mission("build the feature", context=sb.context(has_requirements=False))
        state.success_criteria[:] = []
        crit_tests = SuccessCriterion(description="the test suite passes", verification_method="pytest tests pass")
        state.success_criteria.append(crit_tests)

        w1 = Task(title="Write calc.py", status=TaskStatus.ACTIVE, operation_hint="execute_code",
                  parameters={"tool": "write_file", "arguments": {"path": "calc.py", "content": CALC}})
        w2 = Task(title="Write test_calc.py", status=TaskStatus.ACTIVE, operation_hint="execute_code",
                  parameters={"tool": "write_file", "arguments": {"path": "test_calc.py", "content": TEST_CALC}})
        state.tasks += [w1, w2]
        for t in (w1, w2):
            _perform(sb, state, StepDecision(operation=OperationKind.EXECUTE_CODE, task_id=t.id, rationale="write", confidence=0.9), t)

        assert len(state.artifacts) >= 2, "both deliverables must register as candidates"
        assert not any(a.verified for a in state.artifacts), "candidates start unverified"

        vt = Task(title="Run the suite", status=TaskStatus.ACTIVE, operation_hint="verify",
                  addresses_criterion_ids=[crit_tests.id],
                  parameters={"commands": [f"{sys.executable} -m pytest -q"], "cwd": str(sb.root)})
        state.tasks.append(vt)
        out = _perform(sb, state, _verify_task(vt.id), vt)

        assert out.verification is not None and out.verification.status == PASSED, out.verification.summary if out.verification else "no verification"
        assert state.tests and state.tests[-1].status == PASSED
        assert crit_tests.id in state.tests[-1].criterion_ids

        engine = VerificationEngine(sb.runtime.executive.fabric, state)
        for a in state.artifacts:
            engine.verify_artifact(a)
        engine.verify_criterion(crit_tests)

        gate = mission_completion_check(state)
        assert gate.status == PASSED, f"with real evidence the gate must pass: {gate.summary}"
    finally:
        sb.cleanup()


def test_end_to_end_negative_control_wrong_implementation_is_refused():
    """The same pipeline, a buggy implementation: completion must be refused."""
    sb = Sandbox("e2e-negative-control")
    try:
        state = sb.runtime.new_mission("build the feature", context=sb.context(has_requirements=False))
        state.success_criteria[:] = []
        crit = SuccessCriterion(description="the test suite passes", verification_method="pytest tests pass")
        state.success_criteria.append(crit)

        w1 = Task(title="Write a broken calc.py", status=TaskStatus.ACTIVE, operation_hint="execute_code",
                  parameters={"tool": "write_file", "arguments": {"path": "calc.py", "content": BROKEN_CALC}})
        w2 = Task(title="Write test_calc.py", status=TaskStatus.ACTIVE, operation_hint="execute_code",
                  parameters={"tool": "write_file", "arguments": {"path": "test_calc.py", "content": TEST_CALC}})
        state.tasks += [w1, w2]
        for t in (w1, w2):
            _perform(sb, state, StepDecision(operation=OperationKind.EXECUTE_CODE, task_id=t.id, rationale="write", confidence=0.9), t)

        vt = Task(title="Run the suite", status=TaskStatus.ACTIVE, operation_hint="verify",
                  addresses_criterion_ids=[crit.id],
                  parameters={"commands": [f"{sys.executable} -m pytest -q"], "cwd": str(sb.root)})
        state.tasks.append(vt)
        out = _perform(sb, state, _verify_task(vt.id), vt)

        assert out.verification is None or out.verification.status != PASSED
        assert not crit.satisfied
        gate = mission_completion_check(state)
        assert gate.status != PASSED, "a mission whose tests fail must never complete"
    finally:
        sb.cleanup()


def test_end_to_end_missing_implementation_is_refused():
    """No deliverable at all: the gate must refuse, with nothing registered and nothing verified."""
    sb = Sandbox("e2e-missing-impl")
    try:
        state = sb.runtime.new_mission("build the feature", context=sb.context(has_requirements=False))
        state.success_criteria[:] = []
        state.success_criteria.append(SuccessCriterion(description="the test suite passes", verification_method="pytest tests pass"))

        gate = mission_completion_check(state)

        assert gate.status != PASSED
        assert not state.artifacts
        assert not any(c.satisfied for c in state.success_criteria)
    finally:
        sb.cleanup()


# ======================================================================================
# Defects found by the investigation of this repair (not in the original F1-F3 report)
# ======================================================================================


def test_f2b_program_output_cannot_forge_test_counts(tmp_path):
    """Found while repairing F2: the count parser scanned raw stdout, so a process could report
    its own test results. A one-line script printing "Report: 5 passed, 0 failed" produced a
    PASSED verification with five fabricated executed tests."""
    _write(tmp_path, "prog.py", 'print("Report: 5 passed, 0 failed")\n')
    state = MissionState(objective="forgery")
    engine = VerificationEngine(_fabric(tmp_path), state)

    res = engine.verify_code([f"{sys.executable} prog.py"], cwd=str(tmp_path))

    assert res.status != PASSED, f"program output must not be readable as test evidence: {res.summary}"
    rec = state.tests[-1]
    assert rec.executed == 0, f"no test executed; counts must not be scraped from program output: {rec.counts}"


def test_f2b_framework_is_identified_from_the_command_not_the_output():
    from cogos.verification.test_outcome import detect_framework

    assert detect_framework(f"{sys.executable} -m pytest -q") == "pytest"
    assert detect_framework("pytest -q tests/") == "pytest"
    assert detect_framework(f"{sys.executable} -m unittest discover") == "unittest"
    # The output is written by the process under test and can claim anything.
    assert detect_framework(f"{sys.executable} prog.py") == "unknown"


def test_f2b_parser_is_isolated_and_anchored_on_the_summary_line():
    from cogos.verification.test_outcome import parse_test_output

    assert parse_test_output("=========== 3 passed in 0.01s ===========")["passed"] == 3
    assert parse_test_output("2 failed, 1 passed in 0.10s") == {"passed": 1, "failed": 2, "error": 0, "skipped": 0}
    # A stray mention in a log body is not a result line.
    assert parse_test_output("our suite has 5 passed cases historically\n")["passed"] == 0
    assert parse_test_output("")["passed"] == 0


def test_f2b_an_authorized_expected_zero_run_still_does_not_prove_a_criterion(tmp_path):
    """An expected-zero contract resolves the run; it never demonstrates that required tests ran."""
    state = MissionState(objective="expected zero is not proof")
    engine = VerificationEngine(_fabric(tmp_path), state)
    crit = SuccessCriterion(description="the suite passes", verification_method="pytest tests pass")
    state.success_criteria.append(crit)
    task = Task(title="expected zero", status=TaskStatus.DONE, addresses_criterion_ids=[crit.id])
    state.tasks.append(task)

    code = engine.verify_code([f'{sys.executable} -c "print(1)"'], cwd=str(tmp_path), task_id=task.id, expect_zero_tests=True)
    assert code.status == PASSED, "the contract authorized a zero-execution run"

    res = engine.verify_criterion(crit)
    assert res.status != PASSED, "a run that executed nothing cannot prove the suite passes"
    assert not crit.satisfied


def test_f3b_required_artifacts_declared_by_path_are_matched(tmp_path):
    """Found while repairing F3: required_artifacts compared only ids and names, but the live
    mission declared absolute paths, so even a verified intact artifact could never match."""
    state = MissionState(objective="required by path")
    engine = VerificationEngine(_fabric(tmp_path), state)
    p = _write(tmp_path, "calc.py", CALC)
    art = Artifact(name="calc.py", kind="code", path=str(p))
    state.artifacts.append(art)
    engine.verify_artifact(art)
    state.resources["required_artifacts"] = [str(p)]

    gate = mission_completion_check(state)
    check = next(c for c in gate.checks if c.name == "required_artifacts")

    assert check.status == PASSED, f"a verified intact artifact at the declared path must satisfy it: {check.detail}"


def test_f3b_required_artifact_still_fails_when_the_file_is_unverified(tmp_path):
    """Path matching must not become a way to pass on registration alone."""
    state = MissionState(objective="unverified required artifact")
    p = _write(tmp_path, "calc.py", CALC)
    state.artifacts.append(Artifact(name="calc.py", kind="code", path=str(p)))  # registered, not verified
    state.resources["required_artifacts"] = [str(p)]

    gate = mission_completion_check(state)
    check = next(c for c in gate.checks if c.name == "required_artifacts")

    assert check.status == FAILED, "an unverified candidate must not satisfy a required artifact"


def test_judgement_may_not_upgrade_a_deterministic_negative_observation():
    """Found while repairing F1: a firewall DENIAL produced INCONCLUSIVE, which executive
    judgement then upgraded to PASSED. Model reasoning must not outrank execution evidence."""
    from cogos.schemas.verification import VerificationCheck as _Check
    from cogos.verification import VerificationResult as _Result

    sb = Sandbox("judgement-guard")
    try:
        state = sb.runtime.new_mission("judgement guard", context=sb.context(has_requirements=False))
        task = Task(title="denied thing", status=TaskStatus.ACTIVE)
        state.tasks.append(task)
        result = _Result(
            target_type="task",
            target_id=task.id,
            status=INCONCLUSIVE,
            summary="denied",
            checks=[_Check(name="write_file:abc", status=INCONCLUSIVE, detail="denied by policy", authoritative=True)],
        )

        judged = sb.runtime.executive._judge_verification(state, task, result)

        assert judged.status == INCONCLUSIVE, "an authoritative non-passing observation must stand"
        assert "not judgeable" in judged.summary
    finally:
        sb.cleanup()


def test_judgement_still_resolves_a_genuine_method_gap():
    """The guard must not disable judgement where it is legitimate: a non-machine-checkable
    method is a gap the executive may reason about."""
    from cogos.schemas.verification import VerificationCheck as _Check
    from cogos.verification import VerificationResult as _Result

    sb = Sandbox("judgement-allowed")
    try:
        state = sb.runtime.new_mission("judgement allowed", context=sb.context(has_requirements=False))
        task = Task(title="prose thing", status=TaskStatus.ACTIVE)
        state.tasks.append(task)
        result = _Result(
            target_type="task",
            target_id=task.id,
            status=INCONCLUSIVE,
            summary="method not machine-checkable",
            checks=[_Check(name="method", status=INCONCLUSIVE, detail="not machine-checkable", authoritative=False)],
        )

        judged = sb.runtime.executive._judge_verification(state, task, result)

        assert "not judgeable" not in judged.summary, "judgement must still run where no observation contradicts it"
    finally:
        sb.cleanup()
