"""Acceptance scenarios A–J and the adversarial suite, run as pytest.

Each scenario drives the real runtime end to end in an isolated sandbox with a
scripted executive (offline, deterministic). See docs/cogos/EVALS.md.
"""

from __future__ import annotations

import pytest

from cogos.evaluation.harness import aggregate_metrics, run_scenario
from cogos.evaluation.scenarios import ACCEPTANCE, ADVERSARIAL, ScenarioResult


@pytest.mark.parametrize("name", list(ACCEPTANCE))
def test_acceptance_scenario(name: str) -> None:
    res = run_scenario(name, ACCEPTANCE[name])
    assert res.passed, f"{name}: {res.summary} :: {res.details}"


@pytest.mark.parametrize("name", list(ADVERSARIAL))
def test_adversarial_scenario(name: str) -> None:
    res = run_scenario(name, ADVERSARIAL[name])
    assert res.passed, f"{name}: {res.summary} :: {res.details}"


def test_metrics_aggregate_shape() -> None:
    results = [ScenarioResult(name="B_ambiguous_implementation", passed=True, summary="", checks={"mission_complete": True, "tests_ran_and_passed": True, "criteria_verified": True}, metrics={"human_questions": 0, "test_pass_rate": 1.0})]
    m = aggregate_metrics(results)
    assert m["task_completion"] == 1.0
    assert m["test_pass_rate"] == 1.0
    assert m["context_recovery_accuracy"] is None
