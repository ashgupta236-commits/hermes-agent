#!/usr/bin/env bash
# Mutation audit: revert each guard in a scratch copy and confirm the test named for it fails.
# A test that still passes with its guard removed is not testing that guard.
set -u
SRC=/home/user/hermes-agent
SP=/tmp/claude-0/-home-user-hermes-agent/fb16b1d9-2c4a-5e50-918a-3785ede49a38/scratchpad
WORK="$SP/mutate"
PY="$SRC/.venv/bin/python"
PASS=0; FAIL=0

run_case() {
  local name="$1" test="$2" pyedit="$3"
  rm -rf "$WORK"
  mkdir -p "$WORK"
  cp -r "$SRC/cogos" "$SRC/tests" "$SRC/pyproject.toml" "$WORK/" 2>/dev/null
  # A replacement whose target string has drifted silently does nothing, and the test then
  # "passes" for the most misleading reason available. Checksum the tree and refuse a no-op.
  local before after
  before=$(cd "$WORK" && find cogos -name '*.py' -exec cat {} + | md5sum)
  ( cd "$WORK" && "$PY" - <<PYEOF
import pathlib
$pyedit
PYEOF
  ) || { printf '  %-52s %s\n' "$name" "EDIT-ERROR"; FAIL=$((FAIL+1)); return; }
  after=$(cd "$WORK" && find cogos -name '*.py' -exec cat {} + | md5sum)
  if [ "$before" = "$after" ]; then
    printf '  %-52s %s\n' "$name" "MUTATION DID NOT APPLY  <-- fix the harness, not the test"
    FAIL=$((FAIL+1)); return
  fi
  local out
  case "$test" in
    tests/*) target="$test" ;;
    *)       target="tests/cogos/test_incident_pipeline.py::$test" ;;
  esac
  out=$(cd "$WORK" && PYTHONPATH="$WORK" "$PY" -m pytest "$target" -q -p no:randomly 2>&1 | tail -3)
  if echo "$out" | grep -q "1 passed"; then
    printf '  %-52s %s\n' "$name" "STILL PASSES  <-- test does not cover its guard"; FAIL=$((FAIL+1))
  elif echo "$out" | grep -qE "1 failed|1 error"; then
    printf '  %-52s %s\n' "$name" "fails as expected"; PASS=$((PASS+1))
  else
    printf '  %-52s %s\n' "$name" "UNCLEAR: $(echo "$out" | head -1)"; FAIL=$((FAIL+1))
  fi
}

echo "guard reverted                                        result"
echo "--------------------------------------------------------------------------"

run_case "composed-command detection" "test_ver1_a_composed_command_cannot_forge_test_evidence" \
'p=pathlib.Path("cogos/verification/test_outcome.py");s=p.read_text();s=s.replace("    if _COMPOSED.search(text):\n        return \"unknown\"\n","",1);p.write_text(s)'

run_case "ambiguous-summary detection" "test_rule_ambiguous_output_is_inconclusive_without_a_report" \
'p=pathlib.Path("cogos/verification/test_outcome.py");s=p.read_text();s=s.replace("    ambiguous = len(summary_lines(output or \"\")) > 1","    ambiguous = False",1);p.write_text(s)'

# Repointed: the shadow is now stopped by the import-path guard before the report rule is reached,
# so that test no longer isolates this one. `unittest` has no trusted path at all, which does.
run_case "runner-report requirement" "tests/cogos/test_trust_boundary.py::test_unittest_has_no_trusted_path_and_is_never_authoritative" \
'p=pathlib.Path("cogos/verification/test_outcome.py");s=p.read_text();s=s.replace("    if report_required and not report_backed and framework != \"unknown\":","    if False:",1);p.write_text(s)'

run_case "report-path canonicalisation" "test_r7_two_spellings_of_the_same_file_are_one_artifact" \
'p=pathlib.Path("cogos/executive/loop.py");s=p.read_text();s=s.replace("        try:\n            resolved = path.resolve()\n        except OSError:\n            return None","        resolved = path",1);p.write_text(s)'

run_case "workspace input binding" "test_the_working_tree_a_test_ran_in_is_bound_to_its_receipt" \
'p=pathlib.Path("cogos/verification/engine.py");s=p.read_text();s=s.replace("        if cwd and include_workspace:","        if False:",1);p.write_text(s)'

run_case "run outputs are not inputs" "test_files_the_run_itself_rewrites_are_outputs_not_inputs" \
'p=pathlib.Path("cogos/verification/engine.py");s=p.read_text();s=s.replace("            if key in moved and key not in declared_paths:","            if False:",1);p.write_text(s)'

run_case "vacuous-uncertainty guard" "test_a_mission_that_did_nothing_cannot_satisfy_an_uncertainty_criterion" \
'p=pathlib.Path("cogos/executive/loop.py");s=p.read_text();s=s.replace("            if not state.unknowns and not (state.synthesis or {}).get(\"conclusion\"):\n                return None","            pass",1);p.write_text(s)'

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

# Repointed: an attested, bound, fresh record that executed nothing isolates this guard; the
# expected-zero test is now refused earlier by the authority floor.
run_case "observed-execution requirement" "tests/cogos/test_trust_boundary.py::test_an_attested_record_that_executed_nothing_still_closes_nothing" \
'p=pathlib.Path("cogos/verification/engine.py");s=p.read_text();s=s.replace("        if not record.counts:\n            return True\n        return record.executed > 0","        return True",1);p.write_text(s)'

# Repointed: the old fixture's records are unattested, so the authority floor refuses them before
# scope is consulted. This one is attested and green, and differs only in what it was run for.
run_case "criterion scope binding" "tests/cogos/test_trust_boundary.py::test_an_attested_record_scoped_to_another_criterion_closes_nothing" \
'p=pathlib.Path("cogos/verification/engine.py");s=p.read_text();s=s.replace("                and self._test_addresses(t, criterion.id)\n","",1);p.write_text(s)'

run_case "zero-execution classification" "test_f2_exit_zero_with_zero_collection_is_inconclusive_never_pass" \
'p=pathlib.Path("cogos/verification/engine.py");s=p.read_text();s=s.replace("            if res.error_kind in (\"denied\", \"requires_human\", \"unavailable\"):","            if False:",1);s=s.replace("                outcome = classify_test_run(","                outcome = _legacy(res) if True else classify_test_run(",1);s=s.replace("from cogos.verification.test_outcome import InputVersionGuard, TestRunOutcome, classify_test_run","from cogos.verification.test_outcome import InputVersionGuard, TestRunOutcome, classify_test_run\ndef _legacy(res):\n    from cogos.schemas.common import VerificationStatus as _V\n    return TestRunOutcome(status=_V.PASSED if res.ok else _V.FAILED, reason=\"legacy exit-code rule\")",1);p.write_text(s)'

run_case "judgement authoritative guard" "test_judgement_may_not_upgrade_a_deterministic_negative_observation" \
'p=pathlib.Path("cogos/executive/loop.py");s=p.read_text();s=s.replace("        blocking = [c for c in result.checks if c.authoritative and c.status != VerificationStatus.PASSED]","        blocking = []",1);p.write_text(s)'

run_case "receipt version binding at the gate" "test_g2_no_false_completion_when_the_implementation_is_swapped_after_verification" \
'p=pathlib.Path("cogos/verification/engine.py");s=p.read_text();s=s.replace("    intact: dict[str, Artifact] = {}","    intact: dict[str, Artifact] = {}\n    _DISABLED = True",1);s=s.replace("        judged = [(receipt_inputs_intact(r), r) for r in crs]","        judged = [((True, \"\"), r) for r in crs]",1);s=s.replace("        ok, detail = artifact_integrity(a)","        ok, detail = (True, \"disabled\")",1);p.write_text(s)'

echo "--------------------------------------------------------------------------"
echo "  $PASS guard(s) load-bearing, $FAIL problem(s)"
rm -rf "$WORK"
[ "$FAIL" -eq 0 ]
