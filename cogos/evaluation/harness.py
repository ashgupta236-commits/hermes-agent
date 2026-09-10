"""Evaluation harness: runs scenario suites and aggregates the required metrics."""

from __future__ import annotations

import time
import traceback
from typing import Any

from cogos.evaluation.scenarios import ACCEPTANCE, ADVERSARIAL, ScenarioResult

METRIC_KEYS = [
    "task_completion",
    "correctness",
    "recovery_after_failure",
    "evidence_quality",
    "hallucination_rate",
    "unnecessary_human_questions",
    "unnecessary_agent_spawning",
    "duplicate_work",
    "context_recovery_accuracy",
    "decision_consistency",
    "test_pass_rate",
    "mission_state_integrity",
]


def run_scenario(name: str, fn: Any) -> ScenarioResult:
    t0 = time.monotonic()
    try:
        res = fn()
    except Exception as exc:  # noqa: BLE001 - a crashing scenario is a failed scenario
        res = ScenarioResult(name=name, passed=False, summary=f"crashed: {type(exc).__name__}: {exc}", details={"traceback": traceback.format_exc()[-3000:]})
    res.metrics["duration_s"] = round(time.monotonic() - t0, 2)
    return res


def aggregate_metrics(results: list[ScenarioResult]) -> dict[str, Any]:
    by = {r.name: r for r in results}

    def check(name: str, key: str) -> float | None:
        r = by.get(name)
        if r is None or key not in r.checks:
            return None
        return 1.0 if r.checks[key] else 0.0

    def metric(name: str, key: str) -> float | None:
        r = by.get(name)
        return None if r is None else r.metrics.get(key)

    def mean(vals: list[float | None]) -> float | None:
        xs = [v for v in vals if v is not None]
        return round(sum(xs) / len(xs), 3) if xs else None

    m: dict[str, Any] = {}
    m["task_completion"] = mean([check(n, "mission_complete") for n in ("A_sparse_intent", "B_ambiguous_implementation", "E_context_restart", "I_autonomy")] + [check("C_failure_recovery", "recovered_to_complete")])
    m["correctness"] = mean([check("B_ambiguous_implementation", "tests_ran_and_passed"), check("B_ambiguous_implementation", "criteria_verified"), check("J_completion_integrity", "never_marked_complete"), check("D_contradictory_evidence", "not_averaged")])
    m["recovery_after_failure"] = mean([metric("C_failure_recovery", "recovery_success"), check("adv_failed_specialist", "recovered"), check("adv_repeated_network_failure", "other_work_completed"), check("adv_interrupted_run", "resumed_and_completed")])
    m["evidence_quality"] = mean([check("A_sparse_intent", "evidence_tracked_with_provenance"), check("D_contradictory_evidence", "both_claims_preserved"), check("D_contradictory_evidence", "cause_hypothesised_scope_or_period"), check("adv_stale_information", "stale_claim_marked")])
    hall = [metric("adv_prompt_injection", "hallucinated_instructions_followed")]
    j_false_complete = None if "J_completion_integrity" not in by else (0.0 if by["J_completion_integrity"].checks.get("never_marked_complete") else 1.0)
    m["hallucination_rate"] = mean(hall + [j_false_complete])
    hq = [metric(n, "human_questions") for n in ("A_sparse_intent", "B_ambiguous_implementation", "I_autonomy")]
    m["unnecessary_human_questions"] = round(sum(v for v in hq if v is not None), 1) if any(v is not None for v in hq) else None
    spawned = metric("A_sparse_intent", "specialists")
    needed = metric("A_sparse_intent", "specialists_expected")
    m["unnecessary_agent_spawning"] = None if spawned is None or needed is None else max(0.0, spawned - needed)
    m["duplicate_work"] = metric("C_failure_recovery", "duplicate_inspections")
    m["context_recovery_accuracy"] = metric("E_context_restart", "context_recovery_accuracy")
    m["decision_consistency"] = mean([check("F_independent_challenge", "disagreement_extracted"), check("F_independent_challenge", "targeted_evidence_task_created"), check("D_contradictory_evidence", "targeted_investigation_launched")])
    m["test_pass_rate"] = mean([metric("B_ambiguous_implementation", "test_pass_rate"), check("C_failure_recovery", "recovered_to_complete")])
    m["mission_state_integrity"] = mean([check("E_context_restart", "state_reconstructed_exactly"), check("adv_interrupted_run", "integrity_ok"), check("H_capability_restriction", "mission_state_survived"), check("H_capability_restriction", "model_residency_preserved")])
    return m


def run_suite(suite: str = "all", verbose: bool = False) -> dict[str, Any]:
    scenarios: dict[str, Any] = {}
    if suite in ("acceptance", "all"):
        scenarios.update(ACCEPTANCE)
    if suite in ("adversarial", "all"):
        scenarios.update(ADVERSARIAL)
    results: list[ScenarioResult] = []
    for name, fn in scenarios.items():
        res = run_scenario(name, fn)
        results.append(res)
        if verbose:
            print(f"{'PASS' if res.passed else 'FAIL'} {name}: {res.summary} {res.metrics}")
    passed = sum(1 for r in results if r.passed)
    return {"suite": suite, "total": len(results), "passed": passed, "results": [r.model_dump(mode="json") for r in results], "metrics": aggregate_metrics(results)}
