#!/usr/bin/env bash
# Mutation audit: revert each guard in a scratch copy and confirm the test named for it fails.
# A test that still passes with its guard removed is not testing that guard.
set -u
SRC=/home/user/hermes-agent
SP=/tmp/claude-0/-home-user-hermes-agent/fb16b1d9-2c4a-5e50-918a-3785ede49a38/scratchpad
WORK="$SP/mutate"
PY="$SRC/.venv/bin/python"

run_case() {
  local name="$1" test="$2" pyedit="$3"
  rm -rf "$WORK"
  mkdir -p "$WORK"
  cp -r "$SRC/cogos" "$SRC/tests" "$SRC/pyproject.toml" "$WORK/" 2>/dev/null
  ( cd "$WORK" && "$PY" - <<PYEOF
import pathlib
$pyedit
PYEOF
  ) || { printf '  %-52s %s\n' "$name" "EDIT-FAILED"; return; }
  local out
  out=$(cd "$WORK" && PYTHONPATH="$WORK" "$PY" -m pytest "tests/cogos/test_incident_pipeline.py::$test" -q 2>&1 | tail -3)
  if echo "$out" | grep -q "1 passed"; then
    printf '  %-52s %s\n' "$name" "STILL PASSES  <-- test does not cover its guard"
  elif echo "$out" | grep -qE "1 failed|1 error"; then
    printf '  %-52s %s\n' "$name" "fails as expected"
  else
    printf '  %-52s %s\n' "$name" "UNCLEAR: $(echo "$out" | head -1)"
  fi
}

echo "guard reverted                                        result"
echo "--------------------------------------------------------------------------"

run_case "composed-command detection" "test_ver1_a_composed_command_cannot_forge_test_evidence" \
'p=pathlib.Path("cogos/verification/test_outcome.py");s=p.read_text();s=s.replace("    if _COMPOSED.search(text):\n        return \"unknown\"\n","",1);p.write_text(s)'

run_case "ambiguous-summary detection" "test_a_conftest_cannot_forge_the_runner_summary" \
'p=pathlib.Path("cogos/verification/test_outcome.py");s=p.read_text();s=s.replace("    ambiguous = len(summary_lines(output or \"\")) > 1","    ambiguous = False",1);s=s.replace("    if len(lines) != 1:\n        return counts","    if not lines:\n        return counts",1);p.write_text(s)'

run_case "tests-substrate writable-roots enforcement" "test_fw1_the_test_runner_is_enforced_against_writable_roots_not_only_classified" \
'p=pathlib.Path("cogos/governance/firewall.py");s=p.read_text();s=s.replace("(\"filesystem\", \"shell\", \"git\", \"tests\")","(\"filesystem\", \"shell\", \"git\")",1);p.write_text(s)'

run_case "observation vs verified-task distinction" "test_a_successful_tool_call_is_an_observation_not_a_verified_task" \
'p=pathlib.Path("cogos/executive/loop.py");s=p.read_text();s=s.replace("            target_type=\"observation\",\n            target_id=task.id,","            target_type=\"task\",\n            target_id=task.id,",1);p.write_text(s)'

run_case "phantom-binding detection" "test_ver2_a_phantom_input_binding_does_not_make_a_receipt_permanently_intact" \
'p=pathlib.Path("cogos/verification/engine.py");s=p.read_text();s=s.replace("            if Path(iv.path).is_file():\n                return False, f\"{Path(iv.path).name} did not exist when this ran but exists now\"\n","",1);p.write_text(s)'

run_case "tool-plan execution for VERIFY" "test_f1_tool_backed_verify_executes_its_plan_and_retains_the_result" \
'p=pathlib.Path("cogos/executive/loop.py");s=p.read_text();s=s.replace("            self._execute_task_plan(state, task, out, ledger)\n            out.verification = self._verify(state, task, decision, out)","            out.verification = self._verify(state, task, decision, out)",1);p.write_text(s)'

run_case "artifact candidate registration" "test_f3_an_authorized_executive_write_registers_an_unverified_candidate" \
'p=pathlib.Path("cogos/executive/loop.py");s=p.read_text();s=s.replace("        self._register_written_artifacts(state, call, res, task, before)\n","",1);p.write_text(s)'

run_case "observed-execution requirement" "test_f2b_an_authorized_expected_zero_run_still_does_not_prove_a_criterion" \
'p=pathlib.Path("cogos/verification/engine.py");s=p.read_text();s=s.replace("        if not record.counts:\n            return True\n        return record.executed > 0","        return True",1);p.write_text(s)'

run_case "criterion scope binding" "test_f2_an_unrelated_passing_suite_cannot_prove_a_differently_scoped_criterion" \
'p=pathlib.Path("cogos/verification/engine.py");s=p.read_text();s=s.replace("                and self._test_addresses(t, criterion.id)\n","",1);p.write_text(s)'

run_case "zero-execution classification" "test_f2_exit_zero_with_zero_collection_is_inconclusive_never_pass" \
'p=pathlib.Path("cogos/verification/engine.py");s=p.read_text();s=s.replace("            if res.error_kind in (\"denied\", \"requires_human\", \"unavailable\"):","            if False:",1);s=s.replace("                outcome = classify_test_run(","                outcome = _legacy(res) if True else classify_test_run(",1);s=s.replace("from cogos.verification.test_outcome import InputVersionGuard, TestRunOutcome, classify_test_run","from cogos.verification.test_outcome import InputVersionGuard, TestRunOutcome, classify_test_run\ndef _legacy(res):\n    from cogos.schemas.common import VerificationStatus as _V\n    return TestRunOutcome(status=_V.PASSED if res.ok else _V.FAILED, reason=\"legacy exit-code rule\")",1);p.write_text(s)'

run_case "judgement authoritative guard" "test_judgement_may_not_upgrade_a_deterministic_negative_observation" \
'p=pathlib.Path("cogos/executive/loop.py");s=p.read_text();s=s.replace("        blocking = [c for c in result.checks if c.authoritative and c.status != VerificationStatus.PASSED]","        blocking = []",1);p.write_text(s)'

run_case "receipt version binding at the gate" "test_g2_no_false_completion_when_the_implementation_is_swapped_after_verification" \
'p=pathlib.Path("cogos/verification/engine.py");s=p.read_text();s=s.replace("    intact: dict[str, Artifact] = {}","    intact: dict[str, Artifact] = {}\n    _DISABLED = True",1);s=s.replace("        judged = [(receipt_inputs_intact(r), r) for r in crs]","        judged = [((True, \"\"), r) for r in crs]",1);s=s.replace("        ok, detail = artifact_integrity(a)","        ok, detail = (True, \"disabled\")",1);p.write_text(s)'

rm -rf "$WORK"
