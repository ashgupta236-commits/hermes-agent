"""Tests for the verification engine."""

from __future__ import annotations

import hashlib
import sys

import pytest

from cogos.config import GovernanceConfig
from cogos.governance.firewall import CapabilityFirewall
from cogos.ids import iso_now
from cogos.schemas.beliefs import Claim, ClaimStatus, Contradiction, Evidence, EvidenceKind
from cogos.schemas.common import ActionClass, Provenance, VerificationStatus
from cogos.schemas.decisions import Decision
from cogos.schemas.mission import Artifact, BlockedOperation, HumanRequest, MissionState, SuccessCriterion, Task, TaskStatus
from cogos.schemas.mission import TestRecord as _TestRecord  # aliased so pytest does not try to collect it
from cogos.simulation import Option, Outcome, Scenario, simulate
from cogos.tools import build_default_fabric
from cogos.tools.fabric import ToolContext
from cogos.verification import VerificationEngine, VerificationResult, mission_completion_check, token_overlap

PASSED = VerificationStatus.PASSED
FAILED = VerificationStatus.FAILED
INCONCLUSIVE = VerificationStatus.INCONCLUSIVE


def _state() -> MissionState:
    return MissionState(objective="verify things")


def _fabric(tmp_path):
    return build_default_fabric(CapabilityFirewall(GovernanceConfig(), tmp_path), ToolContext(tmp_path))


def _evidence(summary: str, source: str, lineage: list[str] | None = None, **kw) -> Evidence:
    return Evidence(summary=summary, provenance=Provenance(source=source, lineage=lineage or []), **kw)


def _check(result: VerificationResult, name: str):
    matches = [c for c in result.checks if c.name == name or c.name.endswith(":" + name)]
    assert matches, f"no check named {name!r} in {[c.name for c in result.checks]}"
    return matches[0]


# --- code -----------------------------------------------------------------------------


def test_verify_code_runs_real_pytest(tmp_path):
    (tmp_path / "test_ok.py").write_text("def test_pass():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    state = _state()
    engine = VerificationEngine(_fabric(tmp_path), state)
    cmd = f"{sys.executable} -m pytest -q test_ok.py"
    result = engine.verify_code([cmd])
    assert result.status == PASSED, result.summary
    assert result.target_type == "code"
    assert "1 passed" in result.summary
    assert len(state.tests) == 1 and state.tests[0].status == PASSED and state.tests[0].command == cmd
    assert engine.results == [result]


def test_verify_code_failure_and_cwd(tmp_path):
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "test_bad.py").write_text("def test_fail():\n    assert False\n", encoding="utf-8")
    state = _state()
    engine = VerificationEngine(_fabric(tmp_path), state)
    result = engine.verify_code([f"{sys.executable} -m pytest -q test_bad.py"], cwd=str(sub))
    assert result.status == FAILED
    assert "1 failed" in result.summary
    assert state.tests[-1].status == FAILED
    assert state.tests[-1].command.startswith("cd ")


def test_verify_code_without_fabric_is_inconclusive():
    state = _state()
    result = VerificationEngine(None, state).verify_code()
    assert result.status == INCONCLUSIVE
    assert result.checks[0].detail == "no tool fabric"
    assert result.checks[0].name == "python -m pytest -q"
    assert state.tests[0].status == VerificationStatus.SKIPPED


# --- research ------------------------------------------------------------------------


def test_verify_research_passes_with_independent_primary_evidence():
    state = _state()
    e1 = _evidence("report A", "https://a.example/report", ["a.example"], kind=EvidenceKind.PRIMARY, freshness="2026-01-01")
    e2 = _evidence("report B", "https://b.example/report", ["b.example"], kind=EvidenceKind.PRIMARY)
    claim = Claim(proposition="Widget revenue grew 20% in 2025", status=ClaimStatus.ESTABLISHED, confidence=0.9, evidence_for=[e1.id, e2.id], freshness="2026-01-01")
    state.evidence += [e1, e2]
    state.claims.append(claim)
    result = VerificationEngine(None, state).verify_research()
    assert result.status == PASSED, result.summary
    assert result.target_type == "claim" and result.target_id == claim.id
    assert set(result.evidence_ids) == {e1.id, e2.id}


def test_verify_research_independence_failure():
    state = _state()
    e1 = _evidence("wire copy", "https://a.example/1", ["reuters"])
    e2 = _evidence("syndicated copy", "https://b.example/2", ["reuters"])
    claim = Claim(proposition="The merger closed in March", status=ClaimStatus.SUPPORTED, confidence=0.9, evidence_for=[e1.id, e2.id])
    state.evidence += [e1, e2]
    state.claims.append(claim)
    result = VerificationEngine(None, state).verify_research([claim.id])
    assert result.status == FAILED
    assert _check(result, "independence").status == FAILED
    assert "1 independent root source" in _check(result, "independence").detail
    # below the confidence threshold the same evidence is acceptable
    claim.confidence = 0.6
    assert VerificationEngine(None, state).verify_research([claim.id]).status == PASSED


def test_verify_research_contradiction_failure_and_narrower_proposition_flag():
    state = _state()
    e1 = _evidence("survey", "https://a.example", ["a"], supports_proposition="Customers in Germany prefer blue packaging")
    claim = Claim(proposition="Widget revenue grew 20% in 2025", status=ClaimStatus.SUPPORTED, confidence=0.5, evidence_for=[e1.id])
    other = Claim(proposition="Widget revenue fell in 2025", status=ClaimStatus.SUPPORTED)
    state.evidence.append(e1)
    state.claims += [claim, other]
    state.contradictions.append(Contradiction(claim_ids=[claim.id, other.id], description="growth vs decline"))
    result = VerificationEngine(None, state).verify_research([claim.id])
    assert result.status == FAILED
    assert _check(result, "contradictions").status == FAILED
    consistency = _check(result, "consistency")
    assert consistency.status == INCONCLUSIVE
    assert "narrower proposition" in consistency.detail

    state.contradictions[0].resolved = True
    again = VerificationEngine(None, state).verify_research([claim.id])
    assert again.status == INCONCLUSIVE  # narrower-proposition flag remains
    assert _check(again, "contradictions").status == PASSED


def test_verify_research_missing_evidence_and_unknown_claim():
    state = _state()
    claim = Claim(proposition="Something", evidence_for=["ev_missing"])
    state.claims.append(claim)
    result = VerificationEngine(None, state).verify_research([claim.id, "clm_nope"])
    assert result.status == FAILED
    assert _check(result, "has_evidence").status == FAILED
    assert _check(result, "exists").status == FAILED


# --- data ------------------------------------------------------------------------------


def test_verify_data_schema_anomaly_and_reconciliation():
    schema = {"title": "ledger", "required": ["id", "amount"], "properties": {"id": {"type": "string"}, "amount": {"type": "number"}, "ok": {"type": "boolean"}}}
    good = [{"id": str(i), "amount": 10.0, "ok": True} for i in range(10)]
    engine = VerificationEngine(None, _state())
    result = engine.verify_data(good, schema, {"field": "amount", "expected_total": 100.0, "tolerance": 0.01})
    assert result.status == PASSED, result.summary
    assert result.target_type == "data" and result.target_id == "ledger"

    bad_total = engine.verify_data(good, schema, {"field": "amount", "expected_total": 90.0, "tolerance": 0.01})
    assert bad_total.status == FAILED
    assert _check(bad_total, "reconciliation").status == FAILED
    assert "expected 90" in _check(bad_total, "reconciliation").detail

    invalid = [{"id": 1, "amount": "ten"}, {"amount": 5}]
    bad_schema = engine.verify_data(invalid, schema)
    assert bad_schema.status == FAILED
    detail = _check(bad_schema, "schema").detail
    assert "missing required 'id'" in detail and "expected number" in detail

    spiky = [{"id": str(i), "amount": 10.0 + (i % 3) * 0.1} for i in range(30)] + [{"id": "x", "amount": 10_000.0}]
    anomalous = engine.verify_data(spiky, schema)
    assert anomalous.status == INCONCLUSIVE
    assert "amount=10000" in _check(anomalous, "anomalies").detail


# --- artifacts -------------------------------------------------------------------------


def test_verify_artifact_hashes_file(tmp_path):
    path = tmp_path / "report.md"
    path.write_text("# Findings\nhello\n", encoding="utf-8")
    state = _state()
    artifact = Artifact(name="report", path=str(path))
    state.artifacts.append(artifact)
    result = VerificationEngine(None, state).verify_artifact(artifact)
    assert result.status == PASSED
    assert artifact.verified is True
    assert artifact.content_hash == hashlib.sha256(path.read_bytes()).hexdigest()

    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    empty_art = Artifact(name="empty", path=str(empty))
    assert VerificationEngine(None, state).verify_artifact(empty_art).status == FAILED
    assert empty_art.verified is False

    missing = Artifact(name="ghost", path=str(tmp_path / "nope.txt"))
    assert VerificationEngine(None, state).verify_artifact(missing).status == FAILED
    assert VerificationEngine(None, state).verify_artifact(Artifact(name="nopath")).status == FAILED


# --- criteria --------------------------------------------------------------------------


def test_verify_criterion_test_gating():
    state = _state()
    criterion = SuccessCriterion(description="All unit tests pass", verification_method="run pytest")
    engine = VerificationEngine(None, state)
    result = engine.verify_criterion(criterion)
    assert result.status == FAILED
    assert criterion.satisfied is False
    # a failed attempt is persisted but never cited as evidence on the criterion
    assert criterion.verification_ids == []
    assert state.verification(result.id) is not None

    # Incident F2 (scope binding): a passing run proves something about the criteria it was run
    # *for*. An unbound record — however green, however fresh — is not proof of this criterion.
    state.tests.append(_TestRecord(name="some other suite", status=PASSED, ran_at=iso_now()))
    unbound = engine.verify_criterion(criterion)
    assert unbound.status == FAILED and criterion.satisfied is False

    state.tests.append(_TestRecord(name="pytest", status=PASSED, ran_at=iso_now(), criterion_ids=[criterion.id]))
    again = engine.verify_criterion(criterion)
    assert again.status == PASSED
    assert criterion.satisfied is True
    assert criterion.verification_ids == [again.id]
    assert state.passing_verifications(criterion.verification_ids) == [again]

    # a stale record (older than the criterion) does not count, bound or not
    state.tests = [_TestRecord(name="old", status=PASSED, ran_at="2000-01-01T00:00:00+00:00", criterion_ids=[criterion.id])]
    stale = engine.verify_criterion(criterion)
    assert stale.status == FAILED and criterion.satisfied is False


def test_verify_criterion_artifact_and_evidence_methods(tmp_path):
    state = _state()
    path = tmp_path / "pricing_report.md"
    path.write_text("data", encoding="utf-8")
    artifact = Artifact(name="pricing report", path=str(path), summary="pricing analysis for the launch")
    state.artifacts.append(artifact)
    engine = VerificationEngine(None, state)
    criterion = SuccessCriterion(description="Deliver the pricing report", verification_method="artifact exists")
    assert engine.verify_criterion(criterion).status == FAILED  # artifact not yet verified
    engine.verify_artifact(artifact)
    assert engine.verify_criterion(criterion).status == PASSED and criterion.satisfied

    ev = _evidence("filing", "sec.gov", ["sec"])
    claim = Claim(proposition="Competitor pricing averages 40 USD per seat", status=ClaimStatus.SUPPORTED, evidence_for=[ev.id])
    state.evidence.append(ev)
    state.claims.append(claim)
    ev_criterion = SuccessCriterion(description="Establish competitor pricing per seat", verification_method="evidence from sources")
    res = engine.verify_criterion(ev_criterion)
    assert res.status == PASSED and ev.id in res.evidence_ids
    state.contradictions.append(Contradiction(claim_ids=[claim.id], description="conflict"))
    assert engine.verify_criterion(ev_criterion).status == FAILED and ev_criterion.satisfied is False


def test_verify_criterion_inconclusive_unless_explicit():
    engine = VerificationEngine(None, _state())
    criterion = SuccessCriterion(description="Stakeholders are happy", verification_method="ask around")
    assert engine.verify_criterion(criterion).status == INCONCLUSIVE
    assert criterion.satisfied is False
    assert engine.verify_criterion(criterion, evidence_ok=True).status == PASSED
    assert criterion.satisfied is True
    assert engine.verify_criterion(criterion, evidence_ok=False).status == FAILED
    assert criterion.satisfied is False


# --- decisions -------------------------------------------------------------------------


def test_verify_decision_with_simulation():
    state = _state()
    ev = _evidence("benchmark", "bench", ["bench"])
    state.evidence.append(ev)
    sim = simulate(
        Scenario(
            question="which",
            options=[
                Option(name="alpha", outcomes=[Outcome(name="o", probability=1.0, benefit=10)]),
                Option(name="beta", outcomes=[Outcome(name="o", probability=1.0, benefit=5)]),
            ],
            samples=10,
        )
    )
    engine = VerificationEngine(None, state)
    good = Decision(objective="pick", selected_option="alpha", concise_rationale="alpha has higher EV", decisive_evidence=[ev.id], assumptions=["demand holds"])
    result = engine.verify_decision(good, sim)
    assert result.status == PASSED, result.summary
    assert result.target_type == "decision" and result.target_id == good.decision_id

    deviating = Decision(objective="pick", selected_option="beta", concise_rationale="cheaper to ship", decisive_evidence=[ev.id], assumptions=["x"])
    assert _check(engine.verify_decision(deviating, sim), "simulation_agreement").status == FAILED
    explained = Decision(objective="pick", selected_option="beta", concise_rationale="alpha wins on EV but is irreversible; beta is safer", decisive_evidence=[ev.id], assumptions=["x"])
    assert _check(engine.verify_decision(explained, sim), "simulation_agreement").status == PASSED

    weak = Decision(objective="pick", selected_option="alpha", concise_rationale="", decisive_evidence=["ev_missing"])
    res = engine.verify_decision(weak)
    assert res.status == FAILED
    assert _check(res, "rationale").status == FAILED
    assert _check(res, "decisive_evidence").status == FAILED
    assert _check(res, "assumptions").status == INCONCLUSIVE

    sim.robust_best = False
    sensitive = engine.verify_decision(good, sim)
    assert sensitive.status == INCONCLUSIVE
    assert _check(sensitive, "robustness").detail == "sensitive to assumptions"


# --- tasks -----------------------------------------------------------------------------


def test_verify_task_dispatch(tmp_path):
    (tmp_path / "test_t.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    state = _state()
    engine = VerificationEngine(_fabric(tmp_path), state)

    code_task = Task(title="code", parameters={"test_command": f"{sys.executable} -m pytest -q test_t.py"})
    res = engine.verify_task(code_task)
    assert res.status == PASSED and res.target_type == "task" and code_task.verification_ids == [res.id]
    assert state.tests[-1].task_id == code_task.id

    path = tmp_path / "out.txt"
    path.write_text("x", encoding="utf-8")
    artifact = Artifact(name="out", path=str(path))
    state.artifacts.append(artifact)
    art_task = Task(title="artifact", artifact_ids=[artifact.id, "art_missing"])
    res = engine.verify_task(art_task)
    assert res.status == FAILED and artifact.verified is True
    assert _check(res, "art_missing").status == FAILED

    nothing = engine.verify_task(Task(title="think"))
    assert nothing.status == INCONCLUSIVE and nothing.summary == "no verifiable output declared"


# --- completion gate ---------------------------------------------------------------------


def test_mission_completion_gate_refuses_until_verified(tmp_path):
    state = _state()
    criterion = SuccessCriterion(description="All unit tests pass", verification_method="pytest")
    state.success_criteria.append(criterion)
    engine = VerificationEngine(None, state)

    gate = engine.mission_completion_check(state)
    assert gate.status == FAILED
    assert gate.target_type == "mission"
    assert "success criteria not verified" in gate.summary and "All unit tests pass" in gate.summary

    # satisfied by hand without a verification id still fails the gate
    criterion.satisfied = True
    assert _check(mission_completion_check(state), "success_criteria").status == FAILED

    # Bound to the criterion it is offered as proof of (incident F2: scope binding).
    state.tests.append(_TestRecord(name="pytest", status=PASSED, ran_at=iso_now(), criterion_ids=[criterion.id]))
    engine.verify_criterion(criterion)
    assert mission_completion_check(state).status == PASSED

    # each remaining gate independently blocks completion with a specific message
    state.tests.append(_TestRecord(name="flaky", status=FAILED, ran_at=iso_now()))
    assert "failing tests: flaky" in mission_completion_check(state).summary
    state.tests.pop()

    state.contradictions.append(Contradiction(claim_ids=["c1"], description="x", severity=0.7))
    assert "unresolved contradictions" in mission_completion_check(state).summary
    state.contradictions[0].severity = 0.2
    assert mission_completion_check(state).status == PASSED

    task = Task(title="deploy", status=TaskStatus.ACTIVE)
    state.tasks.append(task)
    state.blocked_operations.append(BlockedOperation(operation="deploy", action_class=ActionClass.CONSEQUENTIAL_SHARED, reason="needs approval", what_would_unblock="human ok", task_id=task.id))
    assert "blocked operations" in mission_completion_check(state).summary
    task.status = TaskStatus.DONE
    assert mission_completion_check(state).status == PASSED

    state.human_requests.append(HumanRequest(kind="decision", question="Which vendor?", why_not_inferable="taste", independent_work_remaining=False))
    assert "Which vendor?" in mission_completion_check(state).summary
    state.human_requests[0].answered = True

    state.resources["required_artifacts"] = ["final report"]
    blocked = mission_completion_check(state)
    assert blocked.status == FAILED and "final report" in blocked.summary
    path = tmp_path / "final.md"
    path.write_text("done", encoding="utf-8")
    artifact = Artifact(name="final report", path=str(path))
    state.artifacts.append(artifact)
    assert mission_completion_check(state).status == FAILED  # declared but unverified
    engine.verify_artifact(artifact)
    final = mission_completion_check(state)
    assert final.status == PASSED and final.summary == "all completion gates satisfied"


def test_token_overlap():
    assert token_overlap("Widget revenue grew", "Widget revenue grew 20% in 2025") == pytest.approx(1.0)
    assert token_overlap("blue packaging in Germany", "Widget revenue grew") == 0.0
    assert token_overlap("", "anything") == 0.0
