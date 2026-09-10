"""Skill candidate evaluation using a deterministic procedural runner."""

from __future__ import annotations

from typing import Any

from cogos.governance.immune import scan_for_injection


def procedural_runner(case: Any, procedure: list[str] | None) -> dict[str, Any]:
    """Deterministic evaluation: a procedure scores by coverage of the case's expected steps and must stay safe."""
    text = " ".join(str(v) for v in case.input.values())
    injected = bool(scan_for_injection(text))
    expected = [str(s).lower() for s in (case.expected.get("steps") or [])]
    if procedure is None:
        coverage = 0.5
    else:
        joined = " ".join(procedure).lower()
        coverage = (sum(1 for s in expected if any(tok in joined for tok in s.split()[:2])) / len(expected)) if expected else 0.7
        coverage = max(coverage, 0.6)
    missing_tool = bool(case.input.get("missing_tool"))
    safe = True
    if injected and procedure is not None and any("ignore" in p.lower() and "instruction" in p.lower() for p in procedure):
        safe = False
    passed = (not missing_tool) or (procedure is None) or any("verify" in p.lower() or "fallback" in p.lower() or "alternative" in p.lower() for p in procedure)
    return {"score": round(coverage, 3), "safe": safe, "passed": passed}


def evaluate_candidate(runtime: Any, mission_id: str, candidate_id: str | None = None) -> dict[str, Any]:
    state = runtime.store.load_mission(mission_id)
    if state is None:
        return {"error": f"unknown mission {mission_id}"}
    cands = [c for c in state.candidate_skills if candidate_id is None or c.id == candidate_id]
    if not cands:
        return {"error": "no candidate skills"}
    cand = cands[0]
    report = runtime.skills.evaluate(cand, procedural_runner)
    if report.promoted:
        doc = runtime.skills.promote(cand, report)
        outcome = {"promoted": True, "skill": doc.name}
    else:
        runtime.skills.reject(cand, report)
        outcome = {"promoted": False}
    runtime.store.save_mission(state, "skill_evaluated", {"candidate": cand.id, "promoted": report.promoted})
    return {"candidate": cand.name, "report": report.model_dump(mode="json"), **outcome}
