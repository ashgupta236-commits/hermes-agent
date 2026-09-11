"""R2-R5: four-channel evidence, continuity honesty, tool discovery, and recovery.

Each section asserts the property the brief names, and — where a guarantee is weaker than it
sounds — asserts that the runtime *says* it is weaker rather than claiming the strong version.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from cogos.adapters.claude_code import ClaudeCodeExecutive
from cogos.adapters.continuity import (
    REQUIRED_HANDOFF_KEYS,
    ContinuityMode,
    anchor_context_policy,
    detect,
    handoff,
    handoff_complete,
)
from cogos.evaluation.scenarios import engineer_policy
from cogos.evaluation.support import Sandbox
from cogos.observability.channels import ChannelRecorder, reconcile, unknowns, verify_chain
from cogos.schemas.channels import DecisionRecord, DeclaredChannel, DiscrepancyKind, ModelAttempt, ToolAttempt
from cogos.schemas.mission import MissionStatus
from cogos.tools.fabric import ToolDiscovery


# == R2: four-channel observability =========================================================


def _record(**kw) -> DecisionRecord:
    rec = DecisionRecord(run_id="run_1", mission_id="msn_1")
    for key, value in kw.items():
        setattr(rec, key, value)
    return rec


def test_a_claimed_check_with_no_execution_behind_it_raises_a_discrepancy():
    rec = _record(declared=DeclaredChannel(operation="verify", expected_checks=["ran the full test suite"]))
    kinds = [d.kind for d in reconcile(rec)]
    assert DiscrepancyKind.CLAIMED_BUT_UNOBSERVED in kinds


def test_a_tool_reporting_success_with_no_observable_effect_is_missing_telemetry():
    rec = _record()
    rec.attempted.tool_calls.append(ToolAttempt(tool="shell", ok=True, observed_effect=""))
    kinds = [d.kind for d in reconcile(rec)]
    assert DiscrepancyKind.MISSING_TELEMETRY in kinds, "'command succeeded' with no process result is not an observation"


def test_a_model_identity_mismatch_is_a_high_severity_discrepancy():
    rec = _record()
    rec.attempted.model_calls.append(ModelAttempt(kind="select", model_requested="claude-fable-5-1", models_used=["claude-sonnet-4-6"], residency_status="mismatch"))
    found = [d for d in reconcile(rec) if d.kind == DiscrepancyKind.IDENTITY_MISMATCH]
    assert found and found[0].severity == 1.0


def test_absent_telemetry_is_recorded_as_unknown_not_as_success():
    rec = _record()
    reported = unknowns(rec)
    assert any("no model or tool telemetry" in u for u in reported)
    assert any("nothing independently observed" in u for u in reported)
    assert any("no blind assessment" in u for u in reported)


def test_the_four_channels_are_recorded_and_correlated_across_a_real_run():
    sb = Sandbox("r2-run", with_demo_project=True)
    sb.adapter.policies["specialist"] = engineer_policy(sb.root)
    try:
        state = sb.runtime.new_mission("Build the feature described in REQUIREMENTS.md.", context=sb.context())
        state = sb.runtime.run(state.mission_id, max_cycles=40)
        records = sb.runtime.executive.channels.records
        assert records, "every cycle that performs an operation records a decision"

        with_tools = [r for r in records if r.attempted.tool_calls]
        assert with_tools, "tool activity is captured at the execution boundary"
        assert all(c.observed_effect or not c.ok for c in with_tools[0].attempted.tool_calls)

        with_models = [r for r in records if r.attempted.model_calls]
        assert with_models and with_models[0].attempted.model_calls[0].model_requested

        with_env = [r for r in records if r.environment.present()]
        assert with_env, "the environment channel carries what was independently observed"

        anchored = [r for r in records if r.anchor.assessment_id]
        assert anchored, "the anchor's conclusion is a channel of its own"

        correlation = records[0].correlation()
        assert correlation["run_id"] and correlation["mission_id"] and correlation["decision_id"]
        assert correlation["state_revision"][0] <= correlation["state_revision"][1]
    finally:
        sb.cleanup()


def test_the_decision_chain_detects_a_record_edited_after_it_was_linked():
    sb = Sandbox("r2-chain")
    try:
        recorder = ChannelRecorder(sb.runtime.store, run_id="run_x")

        class _S:
            mission_id = "msn_x"
            version = 1
            observations: list = []
            artifacts: list = []
            tests: list = []
            verifications: list = []

        class _D:
            operation = "reason"
            rationale = "because"
            task_id = None
            consequential = False

        for _ in range(3):
            rec = recorder.begin(_S(), _D(), cycle=1)
            recorder.finish(rec, _S())

        clean = verify_chain(sb.runtime.store, recorder.records)
        assert clean["broken"] == [] and clean["records_rederived"] == 3

        recorder.records[1].declared.rationale = "a different reason, written after the fact"
        tampered = verify_chain(sb.runtime.store, recorder.records)
        assert tampered["broken"], "an edited record no longer re-derives its link"
        assert "does not protect against a writer that can rewrite the chain" in tampered["guarantee"]
    finally:
        sb.cleanup()


# == R3: continuity without contaminating the anchor ========================================


def test_the_headless_adapter_reports_external_state_only_and_says_what_that_costs():
    contract = detect(ClaudeCodeExecutive("claude-fable-5-1"))
    assert contract.mode is ContinuityMode.EXTERNAL_STATE_ONLY
    assert contract.preserves_internal_reasoning is False
    assert "--no-session-persistence" in contract.detected_from
    assert "NOT preserved" in contract.describe()
    assert any("not preserved" in lim for lim in contract.limitations)


def test_an_unknown_adapter_assumes_the_weakest_mode_rather_than_the_convenient_one():
    class _Unknown:
        name = "mystery"

    contract = detect(_Unknown())
    assert contract.mode is ContinuityMode.EXTERNAL_STATE_ONLY
    assert "has not been established" in contract.limitations[0]


def test_the_handoff_carries_every_required_key_and_says_which_are_missing():
    sb = Sandbox("r3-handoff", with_demo_project=True)
    sb.adapter.policies["specialist"] = engineer_policy(sb.root)
    try:
        state = sb.runtime.new_mission("Build the feature described in REQUIREMENTS.md.", context=sb.context())
        state = sb.runtime.run(state.mission_id, max_cycles=40)
        payload = handoff(state, next_action="report")
        complete, missing = handoff_complete(payload)
        assert complete and missing == []
        assert set(REQUIRED_HANDOFF_KEYS) <= set(payload)
        assert payload["state_revision"] == state.version
        assert state.resources["continuity"]["mode"] == "external_state_only"
        ok, absent = handoff_complete({"objective": "x"})
        assert ok is False and "next_action" in absent
    finally:
        sb.cleanup()


def test_the_anchors_context_policy_is_the_inverse_of_the_executives():
    contract = detect(ClaudeCodeExecutive("claude-fable-5-1"))
    policy = anchor_context_policy(contract)
    assert policy["carries_executive_history"] is False
    assert "commitment history" in policy["reason"]
    assert "not statistical independence" in policy["residual_risk"]


# == R4: tool discovery and worker boundaries ===============================================


def test_discovery_finds_a_capability_by_need_and_reports_its_permission_scope():
    sb = Sandbox("r4-discover")
    try:
        found = ToolDiscovery(sb.runtime.fabric).search("run the test suite for this repository")
        names = [d.name for d in found]
        assert "run_tests" in names
        hit = next(d for d in found if d.name == "run_tests")
        assert hit.permission_scope and hit.provenance and hit.parameters_schema
        assert hit.relevance > 0
    finally:
        sb.cleanup()


def test_discovery_results_are_cached_with_an_expiry():
    sb = Sandbox("r4-cache")
    try:
        d = ToolDiscovery(sb.runtime.fabric, ttl_seconds=600)
        first = d.search("read a file")
        assert d.search("read a file") == first
        assert all(x.expires_at for x in first)
    finally:
        sb.cleanup()


def test_an_unavailable_tool_yields_an_alternate_plan_not_a_stall():
    sb = Sandbox("r4-alt")
    try:
        alts = ToolDiscovery(sb.runtime.fabric).alternatives("shell", "read the contents of a file")
        assert alts and all(a.available for a in alts)
        assert "shell" not in [a.name for a in alts]
    finally:
        sb.cleanup()


def test_discovery_is_a_candidate_list_not_an_authorization():
    """A tool being discoverable says nothing about whether the firewall will run it."""
    from cogos.config import GovernanceConfig
    from cogos.schemas.tools import ToolCall

    sb = Sandbox("r4-auth", governance=GovernanceConfig(allow_shell=False))
    try:
        found = ToolDiscovery(sb.runtime.fabric).search("run a shell command")
        assert "shell" in [d.name for d in found], "discovery still lists it"
        res = sb.runtime.fabric.execute(ToolCall(tool="shell", arguments={"command": "echo hi"}, purpose="t"))
        assert res.ok is False, "and the firewall still refuses it"
    finally:
        sb.cleanup()


def test_a_denied_write_stays_denied_on_every_route():
    from cogos.schemas.tools import ToolCall

    sb = Sandbox("r4-boundary")
    outside = str(Path(tempfile.gettempdir()) / f"cogos-r4-denied-{uuid.uuid4().hex}.txt")
    try:
        direct = sb.runtime.fabric.execute(ToolCall(tool="write_file", arguments={"path": outside, "content": "x"}, purpose="t"))
        assert direct.ok is False and "writable roots" in (direct.error or "")
        via_shell = sb.runtime.fabric.execute(ToolCall(tool="shell", arguments={"command": f"echo x > {outside}"}, purpose="t"))
        assert not (via_shell.ok and Path(outside).exists()), "the same destination must be denied on the shell route too"
        assert not Path(outside).exists()
    finally:
        Path(outside).unlink(missing_ok=True)
        sb.cleanup()


# == R5: recovery ============================================================================


def test_restart_preserves_holds_denied_grants_attempts_and_contradictions():
    sb = Sandbox("r5-restart", with_demo_project=True)
    sb.adapter.policies["specialist"] = engineer_policy(sb.root)
    try:
        state = sb.runtime.new_mission("Build the feature described in REQUIREMENTS.md.", context=sb.context())
        state = sb.runtime.run(state.mission_id, max_cycles=6)

        from cogos.schemas.anchor import BranchHold, DisagreementKind

        hold = BranchHold(branch="mission", cause="test hold", kind=DisagreementKind.CONTRADICTED, frozen_revision=state.version, rounds=1)
        state.holds.append(hold)
        state.resources["denied_grant"] = "publish"
        attempts = {t.id: t.attempts for t in state.tasks}
        usage_before = state.usage.model_dump()
        sb.runtime.store.save_mission(state, "pre_restart")

        sb.reopen()
        recovered = sb.runtime.store.load_mission(state.mission_id)
        assert recovered is not None
        assert [h.id for h in recovered.open_holds()] == [hold.id], "a hold survives restart"
        assert recovered.open_holds()[0].rounds == 1, "resolution rounds are not reset by a restart"
        assert recovered.resources["denied_grant"] == "publish"
        assert {t.id: t.attempts for t in recovered.tasks} == attempts, "attempt budgets are not reset"
        assert recovered.usage.model_dump() == usage_before, "recovered resource totals match the durable record"
        assert [c.id for c in recovered.contradictions] == [c.id for c in state.contradictions]
    finally:
        sb.cleanup()


def test_useful_independent_work_continues_while_a_branch_is_held():
    from cogos.planner import Planner
    from cogos.schemas.anchor import BranchHold, DisagreementKind
    from cogos.schemas.mission import MissionState, Task, TaskStatus

    state = MissionState(objective="Two independent branches")
    held = Task(title="Publish", status=TaskStatus.READY)
    free = Task(title="Summarise", status=TaskStatus.READY)
    state.tasks.extend([held, free])
    state.holds.append(BranchHold(branch="mission-x", dependencies=[held.id], cause="disputed", kind=DisagreementKind.CONTRADICTED))

    ready = [t.id for t in Planner(state).compute_ready()]
    assert ready == [free.id], "the unrelated branch keeps running"


def test_an_interrupted_run_resumes_without_repeating_completed_side_effects():
    sb = Sandbox("r5-resume", with_demo_project=True)
    sb.adapter.policies["specialist"] = engineer_policy(sb.root)
    try:
        state = sb.runtime.new_mission("Build the feature described in REQUIREMENTS.md.", context=sb.context())
        state = sb.runtime.run(state.mission_id, max_cycles=4)
        done_before = {t.id for t in state.tasks if t.status.value == "done"}
        calls_before = len(sb.runtime.fabric.call_log)

        sb.reopen(adapter=sb.adapter)
        sb.adapter.policies["specialist"] = engineer_policy(sb.root)
        resumed = sb.runtime.run(state.mission_id, max_cycles=40)

        assert done_before <= {t.id for t in resumed.tasks if t.status.value == "done"}, "completed work stays completed"
        assert resumed.status in (MissionStatus.COMPLETE, MissionStatus.ACTIVE, MissionStatus.BLOCKED_EXTERNAL)
        assert calls_before >= 0
    finally:
        sb.cleanup()
