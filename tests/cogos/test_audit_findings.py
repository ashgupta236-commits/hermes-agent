"""Regression suite for the seven audited findings (F1-F7).

These are the audit's behavioural counterexamples, rewritten against current production entry
points and asserting the *repaired* behaviour. The historical expected outputs are deliberately
not preserved: each case exists so the defect cannot come back, not to document what it used to
do. Where the repair rejects an invalid fixture earlier than the audit's script did (receipt
binding refuses the citation at insertion), the test asserts the earlier rejection.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from cogos.adapters.base import CognitionRequest
from cogos.adapters.claude_code import ClaudeCodeExecutive
from cogos.evaluation.skill_runner import MeasuredSkillRunner
from cogos.evaluation.support import Sandbox
from cogos.observability.ledger import ResourceLedger
from cogos.schemas.common import VerificationStatus as VS
from cogos.schemas.mission import Artifact, Budget, CandidateSkill, MissionState, ResourceUsage, SuccessCriterion
from cogos.schemas.verification import VerificationResult, cite
from cogos.verification.engine import VerificationEngine, artifact_integrity, mission_completion_check


# -- F4: receipt binding ----------------------------------------------------------------


def test_receipt_for_another_target_is_not_evidence_for_a_criterion():
    state = MissionState(objective="Produce an independently verified report")
    criterion = SuccessCriterion(description="Report conclusions established", satisfied=True)
    state.success_criteria.append(criterion)
    receipt = VerificationResult(target_type="artifact", target_id="unrelated-artifact", status=VS.PASSED, summary="Unrelated file exists")
    VerificationEngine(None, state).record(receipt)

    # Citation refuses the unbound receipt at insertion...
    assert cite(criterion.verification_ids, receipt, criterion.id, target_type="criterion") is False
    assert criterion.verification_ids == []

    # ...and the gate refuses it even if it is forced onto the criterion some other way.
    criterion.verification_ids.append(receipt.id)
    assert state.passing_verifications(criterion.verification_ids, target_type="criterion", target_id=criterion.id) == []
    assert mission_completion_check(state).status == VS.FAILED


# -- F3: artifact integrity -------------------------------------------------------------


def test_deleting_a_verified_artifact_blocks_completion():
    with tempfile.TemporaryDirectory(prefix="cogos-f3-") as d:
        path = Path(d) / "report.txt"
        path.write_text("market analysis findings", encoding="utf-8")
        state = MissionState(objective="Deliver market analysis report")
        artifact = Artifact(name="market analysis report", path=str(path), summary="market analysis report")
        state.artifacts.append(artifact)
        state.resources["required_artifacts"] = [artifact.id]
        criterion = SuccessCriterion(description="market analysis report", verification_method="artifact")
        state.success_criteria.append(criterion)

        engine = VerificationEngine(None, state)
        engine.verify_artifact(artifact)
        engine.verify_criterion(criterion)
        assert mission_completion_check(state).status == VS.PASSED
        assert artifact.verified_hash and artifact_integrity(artifact)[0] is True

        path.unlink()
        gate = mission_completion_check(state)
        assert gate.status == VS.FAILED
        assert "no longer exists" in gate.summary


def test_editing_a_verified_artifact_blocks_completion_until_rechecked():
    with tempfile.TemporaryDirectory(prefix="cogos-f3b-") as d:
        path = Path(d) / "report.txt"
        path.write_text("original findings", encoding="utf-8")
        state = MissionState(objective="Deliver report")
        artifact = Artifact(name="report", path=str(path), summary="report")
        state.artifacts.append(artifact)
        engine = VerificationEngine(None, state)
        engine.verify_artifact(artifact)
        assert mission_completion_check(state).status != VS.FAILED

        path.write_text("silently swapped findings", encoding="utf-8")
        ok, detail = artifact_integrity(artifact)
        assert ok is False and "content changed" in detail
        assert mission_completion_check(state).status == VS.FAILED

        # Re-verification checks the *current* bytes and re-establishes the artifact honestly.
        engine.verify_artifact(artifact)
        assert artifact_integrity(artifact)[0] is True


# -- F5: durable receipts ----------------------------------------------------------------


def test_a_referenced_receipt_is_never_evicted():
    state = MissionState(objective="Long running mission")
    criterion = SuccessCriterion(description="A completed obligation", satisfied=True)
    state.success_criteria.append(criterion)
    engine = VerificationEngine(None, state)

    receipt = engine.record(VerificationResult(target_type="criterion", target_id=criterion.id, status=VS.PASSED, summary="valid initial receipt"))
    assert cite(criterion.verification_ids, receipt, criterion.id, target_type="criterion") is True

    for n in range(engine.MAX_UNREFERENCED_VERIFICATIONS * 2):
        engine.record(VerificationResult(target_type="task", target_id=f"other-{n}", status=VS.PASSED, summary="another check"))

    assert state.verification(receipt.id) is not None, "a cited receipt must stay resolvable"
    assert state.passing_verifications(criterion.verification_ids, target_type="criterion", target_id=criterion.id) == [receipt]
    assert mission_completion_check(state).status == VS.PASSED
    # Unreferenced records are still bounded.
    assert len(state.verifications) <= engine.MAX_UNREFERENCED_VERIFICATIONS + 1


# -- F1: measured skill evaluation --------------------------------------------------------


def test_a_procedure_that_executes_nothing_is_not_promoted():
    sb = Sandbox("f1-empty")
    try:
        candidate = CandidateSkill(
            name="empty-verifier",
            description="A procedure with no executable implementation",
            procedure=["verify the imaginary result"],
        )
        report = sb.runtime.skills.evaluate(candidate, MeasuredSkillRunner(sb.runtime.fabric))
        assert report.promoted is False
        assert report.executed is False and report.measured_cases == 0
        assert any("measured execution" in r for r in report.reasons)
    finally:
        sb.cleanup()


def test_a_procedure_that_really_runs_is_measured_and_can_be_promoted():
    """The gate must reject unmeasured candidates without making promotion unreachable."""
    sb = Sandbox("f1-real")
    try:
        candidate = CandidateSkill(
            name="read-transform-verify",
            description="Read the payload, write the transformed output, prove it with tests",
            procedure=[
                "inspect: read the source payload (tool: read_file)",
                "act: write the transformed result (tool: write_file)",
                "verify: run the checks (tool: run_tests)",
            ],
        )
        report = sb.runtime.skills.evaluate(candidate, MeasuredSkillRunner(sb.runtime.fabric))
        assert report.executed is True and report.measured_cases >= 2
        assert report.skill_score > report.baseline_score
        assert report.promoted is True
    finally:
        sb.cleanup()


def test_a_procedure_that_destroys_the_workspace_is_measured_unsafe():
    """Safety is read off the filesystem, not off the wording of the procedure."""
    sb = Sandbox("f1-unsafe")
    try:
        candidate = CandidateSkill(
            name="destructive",
            description="Deletes its inputs",
            procedure=[
                "inspect: read the source payload (tool: read_file)",
                "act: remove the input (tool: delete_file)",
            ],
        )
        report = sb.runtime.skills.evaluate(candidate, MeasuredSkillRunner(sb.runtime.fabric))
        assert report.promoted is False
        assert report.adversarial_pass_rate < 1.0
    finally:
        sb.cleanup()


# -- F2: grounded judgement ---------------------------------------------------------------


def test_a_confident_judgement_with_no_supporting_material_does_not_satisfy_a_criterion():
    sb = Sandbox("f2-judge")
    try:
        sb.adapter.policies["verify"] = lambda req: {
            "status": "passed",
            "summary": "Everything checked",
            "confidence": 0.99,
            "issues": [],
            "checked": ["Read the complete report and checked all conclusions"],
        }
        state = MissionState(objective="Produce a correct report", executive_model=sb.config.executive.model)
        criterion = SuccessCriterion(description="The report is correct", verification_method="independent reviewer judgment")
        state.success_criteria.append(criterion)
        engine = VerificationEngine(sb.runtime.fabric, state)
        initial = engine.verify_criterion(criterion)
        result = sb.runtime.executive._judge_criterion(state, criterion, initial, engine)

        assert criterion.satisfied is False
        assert result.status == VS.INCONCLUSIVE
        assert any(c.name == "judgement_grounding" for c in result.checks)
        assert mission_completion_check(state).status == VS.FAILED
        assert criterion.id in state.resources["controller"]["undecidable_criteria"]
    finally:
        sb.cleanup()


def test_the_same_judgement_is_accepted_when_real_material_backs_it():
    sb = Sandbox("f2-grounded")
    try:
        sb.adapter.policies["verify"] = lambda req: {
            "status": "passed",
            "summary": "Checked the produced report against the requirement",
            "confidence": 0.95,
            "issues": [],
            "checked": ["Read report.md and confirmed the conclusions"],
        }
        report_path = sb.root / "report.md"
        report_path.write_text("# Report\n\nThe conclusions are stated and sourced.\n", encoding="utf-8")
        state = MissionState(objective="Produce a correct report", executive_model=sb.config.executive.model)
        criterion = SuccessCriterion(description="The report is correct", verification_method="independent reviewer judgment")
        state.success_criteria.append(criterion)
        artifact = Artifact(name="report.md", path=str(report_path), summary="the report")
        state.artifacts.append(artifact)
        engine = VerificationEngine(sb.runtime.fabric, state)
        engine.verify_artifact(artifact)
        initial = engine.verify_criterion(criterion)
        result = sb.runtime.executive._judge_criterion(state, criterion, initial, engine)

        assert criterion.satisfied is True
        assert result.status == VS.PASSED
        assert state.passing_verifications(criterion.verification_ids, target_type="criterion", target_id=criterion.id)
        assert "grounded in" in result.summary
    finally:
        sb.cleanup()


# -- F6: model identity --------------------------------------------------------------------


def test_model_identity_is_verified_mismatched_or_unknown():
    adapter = ClaudeCodeExecutive("claude-fable-5-1", max_retries=1)
    # A second, non-auxiliary model served part of the cognition.
    assert adapter._residency_ok("claude-fable-5-1", ["claude-fable-5-1", "claude-sonnet-4-6"]) is False
    # An older generation of the same family is a downgrade, not a match.
    assert adapter._residency_ok("claude-fable-5-1", ["claude-fable-5"]) is False
    # No telemetry: unknown, which is not the same as verified.
    assert adapter._residency_ok("claude-fable-5-1", []) is None
    # A date-stamped id and an alias resolution are the same model.
    assert adapter._residency_ok("claude-fable-5-1", ["claude-fable-5-1-20260901"]) is True
    assert adapter._residency_ok("fable", ["claude-fable-5-1"]) is True
    # An auxiliary model alone establishes nothing about who did the reasoning.
    assert adapter._residency_ok("claude-fable-5-1", ["claude-haiku-4-5-20251001"]) is False


# -- F7: attempt accounting -----------------------------------------------------------------


def _cli_json(**kw):
    base = {"structured_output": {}, "total_cost_usd": 0.0, "usage": {"input_tokens": 0, "output_tokens": 0}, "modelUsage": {"claude-fable-5-1": {}}}
    base.update(kw)
    return json.dumps(base)


def test_every_billed_attempt_is_accounted_for():
    adapter = ClaudeCodeExecutive("claude-fable-5-1", max_retries=1)
    req = CognitionRequest(kind="select", system_prompt="test", prompt="test", schema_name="test", output_schema={"type": "object"}, model="claude-fable-5-1")
    first = subprocess.CompletedProcess([], 1, _cli_json(is_error=True, result="temporarily overloaded", total_cost_usd=1.0, usage={"input_tokens": 100, "output_tokens": 20}), "")
    second = subprocess.CompletedProcess([], 0, _cli_json(total_cost_usd=2.0, usage={"input_tokens": 200, "output_tokens": 40}), "")

    with patch.object(adapter, "available", return_value=(True, "mock")), \
         patch("cogos.adapters.claude_code.subprocess.run", side_effect=[first, second]) as run, \
         patch("cogos.adapters.claude_code.time.sleep"):
        resp = adapter.call(req)

    assert run.call_count == 2
    assert resp.attempts == 2 and len(resp.attempt_records) == 2
    assert resp.billed_cost_usd() == 3.0

    usage = ResourceUsage()
    ledger = ResourceLedger(usage)
    ledger.add_model_call(resp)
    assert usage.model_calls == 2, "both billed attempts are calls"
    assert usage.retries == 1
    assert usage.estimated_cost_usd == 3.0
    assert usage.input_tokens == 300 and usage.output_tokens == 60


def test_a_reserved_call_counts_against_the_budget_before_it_is_accounted():
    ledger = ResourceLedger(ResourceUsage())
    budget = Budget(max_model_calls=2, max_cost_usd=1.0)
    assert ledger.over_budget(budget) is None

    ledger.reserve(calls=2, cost_usd=0.5)
    assert ledger.over_budget(budget) is not None, "an in-flight call must not be invisible"
    assert ledger.remaining(budget)["model_calls"] == 0

    ledger.release()
    assert ledger.over_budget(budget) is None
