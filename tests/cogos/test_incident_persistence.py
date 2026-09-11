"""Persistence and resume coverage for the incident repair.

The repair adds fields to ``Artifact`` (origin, version history, action provenance),
``TestRecord`` (criterion scope, structured counts) and ``VerificationResult`` (input versions).
All are additive with defaults, so no SQL migration is required — the mission row is a single
``state_json`` blob validated by pydantic. What *does* need proving is that a record written
before the repair is not silently given provenance or verified status it never earned.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cogos.config import CogosConfig, ExecutiveConfig, GovernanceConfig
from cogos.persistence import StateStore
from cogos.schemas.mission import Artifact, ArtifactOrigin, MissionState, SuccessCriterion, Task, TaskStatus
from cogos.schemas.mission import TestRecord as _TestRecord  # aliased so pytest does not try to collect it
from cogos.schemas.common import VerificationStatus
from cogos.schemas.verification import VerificationResult
from cogos.tools import build_default_fabric
from cogos.tools.fabric import ToolContext
from cogos.governance.firewall import CapabilityFirewall
from cogos.verification import VerificationEngine, mission_completion_check

PASSED = VerificationStatus.PASSED
FAILED = VerificationStatus.FAILED
INCONCLUSIVE = VerificationStatus.INCONCLUSIVE

CALC = 'def add_percent(value: float, percent: float) -> float:\n    return round(value * (1 + percent / 100), 2)\n'
TESTS = "from calc import add_percent\n\n\ndef test_p():\n    assert add_percent(100, 10) == 110.0\n"

def _live_snapshots() -> list[Path]:
    """The real Live Run #1/#2 snapshots, when this machine still has them.

    They are workspace scratch, not repository fixtures, so their absence skips rather than fails:
    the synthetic legacy cases above cover the same invariants without depending on them.
    """
    found: list[Path] = []
    for d in (Path("/tmp/cogos-live-run/.cogos/snapshots"), Path("/tmp/cogos-live-run-2/.cogos/snapshots")):
        if d.is_dir():
            found.extend(sorted(d.glob("*.json")))
    return found


LIVE_SNAPSHOTS = _live_snapshots()


def _store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "cogos.db")


def _fabric(root: Path):
    return build_default_fabric(CapabilityFirewall(GovernanceConfig(trust_workspace_code=True), root), ToolContext(root))


def _roundtrip(store: StateStore, state: MissionState) -> MissionState:
    store.save_mission(state)
    loaded = store.load_mission(state.mission_id)
    assert loaded is not None
    return loaded


# --------------------------------------------------------------------------------------
# resume with an unverified candidate
# --------------------------------------------------------------------------------------


def test_resume_artifact_created_but_not_verified(tmp_path):
    store = _store(tmp_path)
    state = MissionState(objective="resume unverified candidate")
    p = tmp_path / "calc.py"
    p.write_text(CALC, encoding="utf-8")
    engine = VerificationEngine(_fabric(tmp_path), state)
    state.artifacts.append(
        Artifact(name="calc.py", path=str(p), content_hash="deadbeef", origin=ArtifactOrigin.MISSION_WRITE, mission_id=state.mission_id, size_bytes=len(CALC))
    )
    state.success_criteria.append(SuccessCriterion(description="calc exists", verification_method="artifact check"))

    loaded = _roundtrip(store, state)

    art = loaded.artifacts[0]
    assert art.verified is False and art.verified_hash is None
    assert art.origin is ArtifactOrigin.MISSION_WRITE
    assert mission_completion_check(loaded).status != PASSED, "an unverified candidate must not survive resume as proof"
    store.close()


def test_resume_preserves_exact_evidence_and_version_identity(tmp_path):
    store = _store(tmp_path)
    state = MissionState(objective="resume with version identity")
    (tmp_path / "calc.py").write_text(CALC, encoding="utf-8")
    (tmp_path / "test_calc.py").write_text(TESTS, encoding="utf-8")
    engine = VerificationEngine(_fabric(tmp_path), state)
    art = Artifact(name="calc.py", path=str(tmp_path / "calc.py"), origin=ArtifactOrigin.MISSION_WRITE)
    state.artifacts.append(art)
    engine.verify_artifact(art)
    crit = SuccessCriterion(description="the suite passes", verification_method="pytest tests pass")
    state.success_criteria.append(crit)
    task = Task(title="run tests", status=TaskStatus.DONE, addresses_criterion_ids=[crit.id])
    state.tasks.append(task)
    receipt = engine.verify_code([f"{sys.executable} -m pytest -q"], cwd=str(tmp_path), task_id=task.id)
    assert receipt.status == PASSED
    engine.verify_criterion(crit)
    assert mission_completion_check(state).status == PASSED

    loaded = _roundtrip(store, state)

    restored = loaded.verification(receipt.id)
    assert restored is not None
    assert restored.input_versions, "input versions must survive the round trip"
    assert {iv.content_hash for iv in restored.input_versions} == {iv.content_hash for iv in receipt.input_versions}
    assert restored.produced_by_action_ids == receipt.produced_by_action_ids
    assert loaded.tests[-1].criterion_ids == [crit.id]
    assert loaded.tests[-1].executed > 0
    assert mission_completion_check(loaded).status == PASSED, "a genuine receipt must still hold after resume"
    store.close()


def test_resume_detects_an_artifact_changed_after_verification(tmp_path):
    store = _store(tmp_path)
    state = MissionState(objective="changed after verification")
    p = tmp_path / "calc.py"
    p.write_text(CALC, encoding="utf-8")
    (tmp_path / "test_calc.py").write_text(TESTS, encoding="utf-8")
    engine = VerificationEngine(_fabric(tmp_path), state)
    art = Artifact(name="calc.py", path=str(p), origin=ArtifactOrigin.MISSION_WRITE)
    state.artifacts.append(art)
    engine.verify_artifact(art)
    crit = SuccessCriterion(description="the suite passes", verification_method="pytest tests pass")
    state.success_criteria.append(crit)
    task = Task(title="run tests", status=TaskStatus.DONE, addresses_criterion_ids=[crit.id])
    state.tasks.append(task)
    engine.verify_code([f"{sys.executable} -m pytest -q"], cwd=str(tmp_path), task_id=task.id)
    engine.verify_criterion(crit)
    assert mission_completion_check(state).status == PASSED

    store.save_mission(state)
    p.write_text("def add_percent(v, p):\n    return 0.0\n", encoding="utf-8")  # swapped after verification
    loaded = store.load_mission(state.mission_id)

    gate = mission_completion_check(loaded)
    assert gate.status != PASSED, "a mutation after verification must be caught on resume"
    assert "artifact" in gate.summary.lower() or "receipt" in gate.summary.lower()
    # The historical record is retained, not deleted.
    assert loaded.verifications, "historical evidence must survive"
    store.close()


def test_resume_preserves_budget_and_task_state(tmp_path):
    store = _store(tmp_path)
    state = MissionState(objective="budget survives resume")
    state.usage.estimated_cost_usd = 7.25
    state.usage.model_calls = 12
    state.usage.cycles = 5
    state.budget.max_cost_usd = 12.0
    state.resources["run_segments"] = [{"started_at_cycle": 0, "spend_at_start_usd": 0.0}]
    t = Task(title="half done", status=TaskStatus.ACTIVE, attempts=2)
    state.tasks.append(t)

    loaded = _roundtrip(store, state)

    assert loaded.usage.estimated_cost_usd == pytest.approx(7.25), "cumulative spend must never reset on resume"
    assert loaded.usage.model_calls == 12 and loaded.usage.cycles == 5
    assert loaded.budget.max_cost_usd == 12.0
    assert loaded.task(t.id).status is TaskStatus.ACTIVE and loaded.task(t.id).attempts == 2
    store.close()


# --------------------------------------------------------------------------------------
# legacy records
# --------------------------------------------------------------------------------------


def test_legacy_record_without_new_fields_loads_without_fabricated_provenance(tmp_path):
    """A mission written before the repair must load, and must not acquire provenance it lacks."""
    legacy = {
        "objective": "legacy mission",
        "artifacts": [{"id": "art_legacy", "name": "calc.py", "path": str(tmp_path / "calc.py"), "verified": True, "content_hash": "abc"}],
        "tests": [{"id": "tst_legacy", "name": "pytest", "status": "passed"}],
        "verifications": [{"id": "ver_legacy", "target_type": "code", "target_id": "task_legacy", "status": "passed", "summary": "legacy"}],
    }
    state = MissionState.model_validate(legacy)

    art = state.artifacts[0]
    assert art.origin is ArtifactOrigin.DECLARED, "a legacy artifact must not claim the mission wrote it"
    assert art.versions == [], "no version history may be invented"
    assert art.produced_by_action_id is None and art.mission_id is None
    assert art.verified_hash is None, "verified without a recorded hash stays unbacked"
    rec = state.tests[0]
    assert rec.criterion_ids == [] and rec.counts == {}, "no scope or counts may be invented"
    ver = state.verifications[0]
    assert ver.input_versions == [], "no input versions may be invented"


def test_a_legacy_verified_artifact_without_a_hash_is_treated_as_unverified(tmp_path):
    """Existing precedent (the F3 audit fix): no recorded hash means nothing to re-check, so the
    gate declines rather than trusting the old claim."""
    state = MissionState(objective="legacy verified artifact")
    p = tmp_path / "calc.py"
    p.write_text(CALC, encoding="utf-8")
    state.artifacts.append(Artifact(id="art_legacy", name="calc.py", path=str(p), verified=True, verified_hash=None))
    state.resources["required_artifacts"] = [str(p)]

    gate = mission_completion_check(state)
    check = next(c for c in gate.checks if c.name == "artifact_integrity")

    assert check.status == FAILED
    assert "without a recorded content hash" in check.detail


def test_a_legacy_receipt_with_no_input_versions_is_not_claimed_unchanged(tmp_path):
    """Empty input_versions means 'unknown', so the receipt-version check has nothing to assert.
    It must not therefore be reported as an affirmative match."""
    state = MissionState(objective="legacy receipt")
    crit = SuccessCriterion(description="thing", verification_method="pytest tests pass")
    state.success_criteria.append(crit)
    res = VerificationResult(target_type="criterion", target_id=crit.id, status=PASSED, summary="legacy")
    state.verifications.append(res)
    crit.verification_ids.append(res.id)
    crit.satisfied = True

    gate = mission_completion_check(state)
    check = next(c for c in gate.checks if c.name == "receipt_input_versions")

    assert check.status == PASSED, "nothing to invalidate"
    assert not res.input_versions, "and nothing was invented to make it look bound"


@pytest.mark.parametrize("snapshot", LIVE_SNAPSHOTS, ids=lambda p: p.name)
def test_real_live_run_snapshots_still_load(snapshot):
    """The actual Live Run #1 and #2 mission snapshots are pre-repair records. They must load
    under the current schema, and must not gain provenance or verified status."""
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    state = MissionState.model_validate(payload["mission"])

    assert state.mission_id
    for a in state.artifacts:
        assert a.origin is ArtifactOrigin.DECLARED and a.versions == []
    for r in state.tests:
        assert r.criterion_ids == [] and r.counts == {}
    for v in state.verifications:
        assert v.input_versions == []
    # The gate still refuses these paused missions, for the reasons they were refused live.
    assert mission_completion_check(state).status != PASSED
