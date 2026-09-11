#!/usr/bin/env bash
# Mutation audit for the isolated behavioural verifier.
#
# Same contract as the other two audits: revert one guard in a scratch copy and require the
# regression named for it to FAIL. Every mutation is checksummed, so a target string that has
# drifted is reported as a harness defect rather than producing a green test for the most
# misleading reason available.
#
# Several guards here are *container flags*. Reverting one and watching the named test fail is the
# only way to know the flag is doing the work the documentation claims — a policy field nobody
# enforces reads exactly like one that is enforced.
#
# Mutation coverage is not proof that untested attacks are impossible.
set -u
SRC=${SRC:-/home/user/hermes-agent}
WORK=${WORK:-/tmp/cogos-isolated-audit}
PY="$SRC/.venv/bin/python"
IV=tests/cogos/test_isolated_verifier.py
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

echo "guard reverted                                          result"
# --- guards added after the adversarial review of this subsystem ----------------------------
run_case "stdin withheld until the subject has imported" "$IV::test_import_time_stdin_hijack_cannot_answer_the_protocol" \
'p=pathlib.Path("cogos/verification/behavioural.py");s=p.read_text();s=s.replace("                    handshake=READY_MARKER,","                    handshake=None,",1);p.write_text(s)'

run_case "the handshake line is not parsed as a response" "$IV::test_the_handshake_line_is_not_read_as_a_response" \
'p=pathlib.Path("cogos/verification/protocol.py");s=p.read_text();s=s.replace("        if message.get(\"ready\") is True and \"request_id\" not in message:","        if False:",1);p.write_text(s)'

run_case "output is streamed, not buffered whole" "$IV::test_a_stdout_flood_does_not_exhaust_the_controller" \
'p=pathlib.Path("cogos/verification/isolation.py");s=p.read_text();s=s.replace("            if len(sink) < cap:\n                sink.extend(chunk[: cap - len(sink)])\n            else:\n                over[0] = True","            sink.extend(chunk)",1);p.write_text(s)'

run_case "teardown happens on every exit path" "$IV::test_no_container_survives_a_batch_of_runs" \
'p=pathlib.Path("cogos/verification/isolation.py");s=p.read_text();s=s.replace("        if proc.poll() is None:\n            proc.kill()\n        _docker(\"kill\", name, timeout=20)\n        _docker(\"rm\", \"-f\", name, timeout=20)","        pass",1);p.write_text(s)'

run_case "subject bytes decoded with replacement" "$IV::test_invalid_utf8_on_stdout_does_not_crash_the_verifier" \
'p=pathlib.Path("cogos/verification/isolation.py");s=p.read_text();s=s.replace("    stdout = bytes(out_buf).decode(\"utf-8\", \"replace\")","    stdout = bytes(out_buf).decode(\"utf-8\")",1);p.write_text(s)'

run_case "float conversion catches OverflowError" "$IV::test_a_huge_integer_result_does_not_crash_the_verifier" \
'p=pathlib.Path("cogos/verification/protocol.py");s=p.read_text();s=s.replace("    except (OverflowError, ValueError):","    except ValueError:",1);p.write_text(s)'

run_case "subject text sanitised before durable state" "$IV::test_an_unpaired_surrogate_cannot_poison_mission_state" \
'p=pathlib.Path("cogos/verification/protocol.py");s=p.read_text();s=s.replace("    return text.encode(\"utf-8\", \"replace\").decode(\"utf-8\", \"replace\")[:limit]","    return text[:limit]",1);p.write_text(s)'

run_case "the isolation policy has a floor" "$IV::test_a_policy_below_the_floor_is_refused" \
'p=pathlib.Path("cogos/verification/isolation.py");s=p.read_text();s=s.replace("        if problems:\n            raise IsolationUnavailable(\"the isolation policy is below the required floor: \" + \"; \".join(problems))","        return",1);p.write_text(s)'

run_case "/dev/shm bounded by the policy" "$IV::test_dev_shm_is_bounded_by_the_policy" \
'p=pathlib.Path("cogos/verification/isolation.py");s=p.read_text();s=s.replace("    shm_size: str = \"16m\"","    shm_size: str = \"256m\"",1);p.write_text(s)'

run_case "the failure flag carries no subject text" "$IV::test_the_failure_flag_carries_no_subject_authored_text" \
'p=pathlib.Path("cogos/verification/isolation.py");s=p.read_text();s=s.replace("    failure = \"exit 125: the container may not have started, or the subject chose this code\" if exit_code == 125 else \"\"","    failure = stderr.strip()[:300] if exit_code == 125 else \"\"",1);p.write_text(s)'

run_case "symlinked deliverables refused" "$IV::test_a_symlinked_deliverable_is_refused" \
'p=pathlib.Path("cogos/verification/behavioural.py");s=p.read_text();s=s.replace("        if source.is_symlink():","        if False:",1);p.write_text(s)'

run_case "suite differential does not fail open on 125" "$IV::test_the_suite_differential_does_not_fail_open_on_an_infrastructure_failure" \
'p=pathlib.Path("cogos/verification/behavioural.py");s=p.read_text();s=s.replace("            discriminates = truth == 0 and mutant not in (0, None, 125) and not infrastructure","            discriminates = truth == 0 and mutant not in (0, None)",1);p.write_text(s)'

run_case "parametrized rows counted as the cases they are" "$IV::test_an_idiomatic_parametrized_suite_is_accepted" \
'p=pathlib.Path("cogos/verification/behavioural.py");s=p.read_text();s=s.replace("                cases = int(observed) if isinstance(observed, int) else len(found)","                cases = len(found)",1);p.write_text(s)'

run_case "the suite runs see the whole workspace" "$IV::test_a_suite_whose_cases_live_in_a_fixture_is_reported_as_undeterminable" \
'p=pathlib.Path("cogos/verification/behavioural.py");s=p.read_text();s=s.replace("                    for name in suite_digests:","                    for name in snapshot.file_digests:",1);p.write_text(s)'

run_case "the contract digest covers its limitations" "$IV::test_the_contract_digest_covers_its_limitations" \
'p=pathlib.Path("cogos/verification/contract.py");s=p.read_text();s=s.replace("                \"limitations\": list(self.limitations),","",1);p.write_text(s)'

echo "----------------------------------------------------------------------------"

# --- the trusted comparison ---------------------------------------------------------------
run_case "trusted comparison of observed vs expected" "$IV::test_a_wrong_implementation_fails_every_comparison" \
'p=pathlib.Path("cogos/verification/contract.py");s=p.read_text();s=s.replace("        return abs(observed - case.expected) <= self.tolerance","        return True",1);p.write_text(s)'

run_case "derived cases drawn from the held seed" "$IV::test_the_contract_is_deterministic_and_seed_bound" \
'p=pathlib.Path("cogos/verification/contract.py");s=p.read_text();s=s.replace("    rng = random.Random(seed)","    rng = random.Random(0)",1);p.write_text(s)'

# --- the bounded protocol -----------------------------------------------------------------
run_case "responses must name an issued request" "$IV::test_a_response_to_an_unissued_request_is_refused" \
'p=pathlib.Path("cogos/verification/protocol.py");s=p.read_text();s=s.replace("        if not isinstance(request_id, str) or request_id not in expected:","        if not isinstance(request_id, str):",1);p.write_text(s)'

run_case "replayed responses refused" "$IV::test_a_replayed_response_is_refused" \
'p=pathlib.Path("cogos/verification/protocol.py");s=p.read_text();s=s.replace("        if request_id in transcript.responses:","        if False:",1);p.write_text(s)'

run_case "duplicate JSON keys refused" "$IV::test_duplicate_keys_are_refused_rather_than_silently_resolved" \
'p=pathlib.Path("cogos/verification/protocol.py");s=p.read_text();s=s.replace("        if key in seen:\n            # A duplicate key means two different readers of this message can disagree about what it\n            # says, which is exactly the ambiguity a wire format must not have.\n            raise ProtocolError(f\"duplicate key {key!r}\")","        if False:\n            pass",1);p.write_text(s)'

run_case "results must be finite numbers in range" "$IV::test_non_finite_or_non_numeric_results_are_refused" \
'p=pathlib.Path("cogos/verification/protocol.py");s=p.read_text();s=s.replace("    if not math.isfinite(number) or abs(number) > MAX_ABS_VALUE:\n        return None","    pass",1);p.write_text(s)'

run_case "line and stream size bounds" "$IV::test_oversized_lines_and_streams_are_bounded" \
'p=pathlib.Path("cogos/verification/protocol.py");s=p.read_text();s=s.replace("        if len(line.encode(\"utf-8\", \"replace\")) > MAX_LINE_BYTES:","        if False:",1);s=s.replace("    if len(lines) > MAX_RESPONSES:","    if False:",1);p.write_text(s)'

# --- the execution boundary ---------------------------------------------------------------
run_case "network disabled" "$IV::test_the_subject_cannot_reach_the_network_or_instance_metadata" \
'p=pathlib.Path("cogos/verification/isolation.py");s=p.read_text();s=s.replace("    network: str = \"none\"","    network: str = \"bridge\"",1);p.write_text(s)'

run_case "source mounted read-only" "$IV::test_the_source_mount_is_read_only_in_effect" \
'p=pathlib.Path("cogos/verification/isolation.py");s=p.read_text();s=s.replace("f\"{snapshot}:{SUBJECT_MOUNT}:ro\"","f\"{snapshot}:{SUBJECT_MOUNT}:rw\"",1);p.write_text(s)'

run_case "container rootfs read-only" "$IV::test_writes_outside_scratch_are_refused" \
'p=pathlib.Path("cogos/verification/isolation.py");s=p.read_text();s=s.replace("    read_only_rootfs: bool = True","    read_only_rootfs: bool = False",1);p.write_text(s)'

run_case "capabilities dropped" "$IV::test_capabilities_are_dropped_even_for_a_root_subject" \
'p=pathlib.Path("cogos/verification/isolation.py");s=p.read_text();s=s.replace("    drop_all_capabilities: bool = True","    drop_all_capabilities: bool = False",1);p.write_text(s)'

run_case "no-new-privileges" "$IV::test_the_subject_runs_unprivileged_with_no_capabilities" \
'p=pathlib.Path("cogos/verification/isolation.py");s=p.read_text();s=s.replace("    no_new_privileges: bool = True","    no_new_privileges: bool = False",1);p.write_text(s)'

run_case "non-root subject user" "$IV::test_the_subject_runs_unprivileged_with_no_capabilities" \
'p=pathlib.Path("cogos/verification/isolation.py");s=p.read_text();s=s.replace("    user: str = \"65534:65534\"","    user: str = \"0:0\"",1);p.write_text(s)'

run_case "memory and process limits" "$IV::test_resource_exhaustion_is_bounded_not_fatal_to_the_controller" \
'p=pathlib.Path("cogos/verification/isolation.py");s=p.read_text();s=s.replace("    memory: str = \"256m\"","    memory: str = \"4g\"",1);s=s.replace("    pids_limit: int = 64","    pids_limit: int = 4096",1);p.write_text(s)'

run_case "scratch tmpfs size cap" "$IV::test_scratch_is_writable_and_bounded" \
'p=pathlib.Path("cogos/verification/isolation.py");s=p.read_text();s=s.replace("    scratch_size: str = \"16m\"","    scratch_size: str = \"512m\"",1);p.write_text(s)'

run_case "truncation is reported to the caller" "$IV::test_resource_exhaustion_is_bounded_not_fatal_to_the_controller" \
'p=pathlib.Path("cogos/verification/isolation.py");s=p.read_text();s=s.replace("            else:\n                over[0] = True","            else:\n                pass",1);p.write_text(s)'

run_case "the wall-clock deadline bounds the run" "$IV::test_a_hanging_subject_is_killed_and_leaves_no_descendants" \
'p=pathlib.Path("cogos/verification/isolation.py");s=p.read_text();s=s.replace("    deadline = started + policy.wall_clock_seconds","    deadline = started + 100000",1);p.write_text(s)'

run_case "an unavailable boundary refuses, never degrades" "$IV::test_a_missing_runtime_image_refuses_rather_than_degrading" \
'p=pathlib.Path("cogos/verification/isolation.py");s=p.read_text();s=s.replace("    raise IsolationUnavailable(\n        f\"the runtime image {policy.image!r} is not present. Build it with: {BUILD_COMMAND}\"\n    )","    return \"\"",1);p.write_text(s)'

# --- snapshot, structure and the suite differential ----------------------------------------
run_case "oversized snapshots refused, never empty-bound" "$IV::test_a_snapshot_that_cannot_be_bound_is_refused_not_silently_empty" \
'p=pathlib.Path("cogos/verification/behavioural.py");s=p.read_text();s=s.replace("        if total > MAX_SNAPSHOT_BYTES:","        if False:",1);p.write_text(s)'

run_case "structural read is bounded" "$IV::test_structural_reading_is_bounded_and_never_executes" \
'p=pathlib.Path("cogos/verification/behavioural.py");s=p.read_text();s=s.replace("    if len(raw) > MAX_SOURCE_BYTES:\n        return {\"ok\": False, \"error\": f\"source exceeds {MAX_SOURCE_BYTES} bytes\"}","    pass",1);p.write_text(s)'

run_case "structural coverage checks" "$IV::test_tests_missing_a_required_percent_sign_are_caught" \
'p=pathlib.Path("cogos/verification/behavioural.py");s=p.read_text();s=s.replace("                    \"passed\": not missing,","                    \"passed\": True,",1);p.write_text(s)'

run_case "suite differential catches a vacuous suite" "$IV::test_a_vacuous_suite_is_caught_by_the_differential" \
'p=pathlib.Path("cogos/verification/behavioural.py");s=p.read_text();s=s.replace("            discriminates = truth == 0 and mutant not in (0, None, 125) and not infrastructure","            discriminates = truth == 0",1);p.write_text(s)'

# --- receipts and the acceptance path ------------------------------------------------------
run_case "receipts bound to the approved contract digest" "$IV::test_a_receipt_from_another_contract_cannot_close_a_criterion" \
'p=pathlib.Path("cogos/verification/engine.py");s=p.read_text();s=s.replace("        if approved and cited != approved:","        if False:",1);p.write_text(s)'

run_case "the acceptance mapping is the only path" "$IV::test_a_forged_test_record_cannot_close_a_contract_mapped_criterion" \
'p=pathlib.Path("cogos/verification/engine.py");s=p.read_text();s=s.replace("        if mapped:\n            checks.extend(self._acceptance_checks(criterion, mapped, created_ms))\n        elif re.search","        if False:\n            pass\n        elif re.search",1);p.write_text(s)'

run_case "a mapped predicate the receipt lacks blocks" "$IV::test_a_receipt_missing_a_mapped_predicate_does_not_partially_satisfy" \
'p=pathlib.Path("cogos/verification/engine.py");s=p.read_text();s=s.replace("            if not covering:\n                out.append(VerificationCheck(name=f\"acceptance:{predicate}\", status=FAILED, detail=\"the approved contract maps this criterion to a predicate the receipt does not carry\", authoritative=True))\n                continue","            if not covering:\n                continue",1);p.write_text(s)'

run_case "artifacts verified only when the contract decided" "$IV::test_a_failed_contract_run_does_not_mark_deliverables_verified" \
'p=pathlib.Path("cogos/verification/engine.py");s=p.read_text();s=s.replace("            decided = status == PASSED","            decided = True",1);p.write_text(s)'

# Repointed: the artifact-integrity check also catches a swap, so that test does not isolate the
# receipt's own input binding. This one asserts the binding directly.
run_case "receipts bind the snapshot bytes" "$IV::test_receipts_name_the_snapshot_contract_policy_and_image" \
'p=pathlib.Path("cogos/verification/engine.py");s=p.read_text();s=s.replace("        snapshot_files = dict(outcome.provenance.get(\"snapshot_files\") or {})","        snapshot_files = {}",1);p.write_text(s)'

echo "----------------------------------------------------------------------------"
echo "  $PASS guard(s) load-bearing, $FAIL problem(s)"
rm -rf "$WORK"
[ "$FAIL" -eq 0 ]
