#!/usr/bin/env bash
# Mutation audit for the trust-boundary repair.
#
# For each guard: revert it in a scratch copy of the tree and require the regression named for it
# to FAIL. A test that still passes with its guard removed is not testing that guard, and a
# mutation that silently does not apply makes a test look covered for the most misleading reason
# available — so every edit is checksummed and a no-op is reported as a harness defect.
#
# Mutation coverage is not proof that untested attacks are impossible. It only establishes that
# each named control is load-bearing for the test that claims to cover it.
set -u
SRC=${SRC:-/home/user/hermes-agent}
WORK=${WORK:-/tmp/cogos-mutation-audit}
PY="$SRC/.venv/bin/python"
PASS=0; FAIL=0

run_case() {
  local name="$1" testpath="$2" pyedit="$3"
  rm -rf "$WORK"; mkdir -p "$WORK"
  cp -r "$SRC/cogos" "$SRC/tests" "$SRC/pyproject.toml" "$WORK/" 2>/dev/null
  local before after
  before=$(cd "$WORK" && find cogos -name '*.py' -exec cat {} + | md5sum)
  ( cd "$WORK" && "$PY" - <<PYEOF
import pathlib
$pyedit
PYEOF
  ) || { printf '  %-54s %s\n' "$name" "EDIT-ERROR"; FAIL=$((FAIL+1)); return; }
  after=$(cd "$WORK" && find cogos -name '*.py' -exec cat {} + | md5sum)
  if [ "$before" = "$after" ]; then
    printf '  %-54s %s\n' "$name" "MUTATION DID NOT APPLY  <-- fix the harness, not the test"
    FAIL=$((FAIL+1)); return
  fi
  local out
  out=$(cd "$WORK" && PYTHONPATH="$WORK" "$PY" -m pytest "$testpath" -q -p no:randomly 2>&1 | tail -3)
  if echo "$out" | grep -qE "[0-9]+ failed|[0-9]+ error"; then
    printf '  %-54s %s\n' "$name" "fails as expected"; PASS=$((PASS+1))
  elif echo "$out" | grep -q "passed"; then
    printf '  %-54s %s\n' "$name" "STILL PASSES  <-- test does not cover its guard"; FAIL=$((FAIL+1))
  else
    printf '  %-54s %s\n' "$name" "UNCLEAR: $(echo "$out" | head -1)"; FAIL=$((FAIL+1))
  fi
}

TB=tests/cogos/test_trust_boundary.py
echo "guard reverted                                          result"
echo "----------------------------------------------------------------------------"

# --- the trusted verifier path ---------------------------------------------------------
run_case "trusted invocation replaced by the plan's command" "$TB::test_an_honest_workspace_with_a_correct_implementation_completes" \
'p=pathlib.Path("cogos/verification/engine.py");s=p.read_text();s=s.replace("            if framework in REPORTING_FRAMEWORKS and under_test is not None:","            if False:",1);p.write_text(s)'

# `-P` on the interpreter and PYTHONSAFEPATH in the environment are one guard expressed twice;
# reverting either alone leaves the other in place, so the audit reverts the pair.
run_case "workspace kept off the import path" "$TB::test_a_workspace_pytest_module_cannot_impersonate_the_runner" \
'p=pathlib.Path("cogos/verification/attestation.py");s=p.read_text();s=s.replace("    env[\"PYTHONSAFEPATH\"] = \"1\"\n","",1);s=s.replace("        \"-P\",\n","",1);p.write_text(s)'

run_case "engine-authored pytest config (-c)" "$TB::test_workspace_pytest_ini_addopts_cannot_inject_a_plugin" \
'p=pathlib.Path("cogos/verification/attestation.py");s=p.read_text();s=s.replace("        \"-c\",\n        str(config),\n","",1);p.write_text(s)'

run_case "ambient environment scrubbed" "$TB::test_pytest_addopts_in_the_ambient_environment_cannot_inject_a_plugin" \
'p=pathlib.Path("cogos/verification/attestation.py");s=p.read_text();s=s.replace("    env = {k: v for k, v in os.environ.items() if k not in _SCRUB_ENV}","    env = dict(os.environ)",1);p.write_text(s)'

run_case "fail-closed default for same-process evidence" "$TB::test_the_default_refuses_in_process_evidence_outright" \
'p=pathlib.Path("cogos/verification/engine.py");s=p.read_text();s=s.replace("        if not self._workspace_code_trusted():","        if False:",1);p.write_text(s)'

# --- the differential control -----------------------------------------------------------
run_case "differential control" "$TB::test_a_conftest_that_flips_outcomes_cannot_complete" \
'p=pathlib.Path("cogos/verification/attestation.py");s=p.read_text();s=s.replace("        survivors = sorted(passed_keys & parsed.passed_keys())","        survivors = []",1);p.write_text(s)'

run_case "control requires every identity to reappear" "$TB::test_a_hostile_test_module_cannot_complete" \
'p=pathlib.Path("cogos/verification/attestation.py");s=p.read_text();s=s.replace("        absent = sorted(passed_keys - control_keys)","        absent = []",1);s=s.replace("        survivors = sorted(passed_keys & parsed.passed_keys())","        survivors = []",1);p.write_text(s)'

# --- report integrity --------------------------------------------------------------------
run_case "report internal consistency" "$TB::test_a_report_that_contradicts_its_own_testcases_is_refused" \
'p=pathlib.Path("cogos/verification/attestation.py");s=p.read_text();s=s.replace("    if total != len(cases):","    if False:",1);p.write_text(s)'

run_case "report scope against the tree and selection" "$TB::test_a_report_about_another_suite_is_refused" \
'p=pathlib.Path("cogos/verification/attestation.py");s=p.read_text();s=s.replace("def scope_problems(report: JUnitReport, cwd: Optional[Path], selection: Sequence[str] = ()) -> list[str]:","def scope_problems(report: JUnitReport, cwd: Optional[Path], selection: Sequence[str] = ()) -> list[str]:\n    return []",1);p.write_text(s)'

run_case "report freshness against the launch time" "$TB::test_a_report_older_than_the_run_is_refused" \
'p=pathlib.Path("cogos/verification/attestation.py");s=p.read_text();s=s.replace("        if report.stat().st_mtime < not_before - 1.0:","        if False:",1);p.write_text(s)'

run_case "ambiguity guard independent of the report" "$TB::test_a_forged_report_does_not_switch_off_the_multi_summary_guard" \
'p=pathlib.Path("cogos/verification/test_outcome.py");s=p.read_text();s=s.replace("    ambiguous = len(summary_lines(output or \"\")) > 1","    ambiguous = (not report_backed) and len(summary_lines(output or \"\")) > 1",1);p.write_text(s)'

run_case "report required for every recognised runner" "$TB::test_unittest_has_no_trusted_path_and_is_never_authoritative" \
'p=pathlib.Path("cogos/verification/engine.py");s=p.read_text();s=s.replace("                    report_required=framework in KNOWN_FRAMEWORKS,","                    report_required=framework in REPORTING_FRAMEWORKS,",1);p.write_text(s)'

# --- evidence authority -------------------------------------------------------------------
run_case "unlabelled authority defaults to the weakest level" "$TB::test_a_record_with_no_authority_deserialises_to_the_weakest_level" \
'p=pathlib.Path("cogos/verification/attestation.py");s=p.read_text();s=s.replace("        return EvidenceAuthority.UNTRUSTED_SELF_REPORT\n\n\ndef at_least","        return EvidenceAuthority.TRUSTED_HARNESS\n\n\ndef at_least",1);p.write_text(s)'

run_case "criterion requires the behavioural authority floor" "$TB::test_evidence_below_the_floor_cannot_close_a_behavioural_criterion" \
'p=pathlib.Path("cogos/verification/engine.py");s=p.read_text();s=s.replace("            fresh = [t for t in scoped if self._test_attested(t)]","            fresh = list(scoped)",1);p.write_text(s)'

run_case "judgement grounding requires the same floor" "$TB::test_an_unrelated_artifact_does_not_ground_a_judgement" \
'p=pathlib.Path("cogos/executive/loop.py");s=p.read_text();s=s.replace("            if CONTENT_SCOPE not in (a.verified_scope or \"\"):\n                # Existence is not grounding. Without this an unrelated, or merely present, file\n                # supported a confident judgement about a criterion it says nothing about.\n                continue\n","",1);s=s.replace("            if token_overlap(criterion.description, f\"{a.name} {a.summary}\") < 0.2:\n                continue\n","",1);p.write_text(s)'

# --- process authorization -------------------------------------------------------------
run_case "unanalysable shell forms refused" "$TB::test_write_target_escapes_do_not_write_outside_the_writable_roots" \
'p=pathlib.Path("cogos/governance/firewall.py");s=p.read_text();s=s.replace("def unanalysable_command(command: str) -> str:","def unanalysable_command(command: str) -> str:\n    return \"\"",1);p.write_text(s)'

run_case "variable-expanded destructive forms refused" "$TB::test_a_variable_expanded_deletion_does_not_delete" \
'p=pathlib.Path("cogos/governance/firewall.py");s=p.read_text();s=s.replace("def unanalysable_command(command: str) -> str:","def unanalysable_command(command: str) -> str:\n    return \"\"",1);p.write_text(s)'

run_case "credential globs classified as credential reads" "$TB::test_credential_reads_do_not_return_the_secret" \
'p=pathlib.Path("cogos/governance/firewall.py");s=p.read_text();s=s.replace("    if _CREDENTIAL.search(c) or _CREDENTIAL_GLOB.search(c):","    if _CREDENTIAL.search(c):",1);s=s.replace("def unanalysable_command(command: str) -> str:","def unanalysable_command(command: str) -> str:\n    return \"\"",1);p.write_text(s)'

run_case "git indirection refused" "$TB::test_a_git_alias_cannot_execute_an_arbitrary_program" \
'p=pathlib.Path("cogos/governance/firewall.py");s=p.read_text();s=s.replace("def git_indirection(command: str) -> str:","def git_indirection(command: str) -> str:\n    return \"\"",1);p.write_text(s)'

run_case "allow_shell covers every spawning substrate" "$TB::test_disabling_shell_disables_every_process_spawning_substrate" \
'p=pathlib.Path("cogos/governance/firewall.py");s=p.read_text();s=s.replace("if spec.substrate in (\"shell\", \"tests\", \"git\") and not cfg.allow_shell:","if spec.substrate in (\"shell\", \"tests\") and not cfg.allow_shell:",1);p.write_text(s)'

# --- binding, artifacts and the gate ------------------------------------------------------
run_case "oversized tree reported instead of silently unbound" "$TB::test_a_tree_too_large_to_bind_is_inconclusive_not_silently_unbound" \
'p=pathlib.Path("cogos/verification/engine.py");s=p.read_text();s=s.replace("        if under_test is not None and workspace_binding_abandoned(under_test):","        if False:",1);p.write_text(s)'

run_case "artifact needs a declared content expectation" "$TB::test_a_placeholder_file_does_not_satisfy_a_criterion" \
'p=pathlib.Path("cogos/verification/engine.py");s=p.read_text();s=s.replace("            checked = [a for a in matches if CONTENT_SCOPE in (a.verified_scope or \"\")]","            checked = list(matches)",1);p.write_text(s)'

run_case "declared expectation actually checked" "$TB::test_a_declared_content_expectation_is_what_makes_an_artifact_evidence" \
'p=pathlib.Path("cogos/verification/engine.py");s=p.read_text();s=s.replace("            for check in _expectation_checks(artifact.expectation, path):\n                checks.append(check)\n","",1);p.write_text(s)'

run_case "unjustified gate skips block completion" "$TB::test_a_check_that_skips_without_declaring_why_blocks_completion" \
'p=pathlib.Path("cogos/verification/engine.py");s=p.read_text();s=s.replace("    unjustified = [c for c in checks if c.status == SKIPPED and not (c.detail or \"\").startswith(INAPPLICABLE)]","    unjustified = []",1);p.write_text(s)'

run_case "required deliverables derived from compilation" "$TB::test_required_deliverables_come_from_the_compiled_mission" \
'p=pathlib.Path("cogos/mission/compiler.py");s=p.read_text();s=s.replace("    if comp.mission_kind not in (\"implementation\", \"repair\"):\n        return out","    return out",1);p.write_text(s)'

echo "----------------------------------------------------------------------------"
echo "  $PASS guard(s) load-bearing, $FAIL problem(s)"
rm -rf "$WORK"
[ "$FAIL" -eq 0 ]
