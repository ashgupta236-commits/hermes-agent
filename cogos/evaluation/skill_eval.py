"""Skill candidate evaluation.

The candidate is evaluated by *running* it: :class:`~cogos.evaluation.skill_runner.MeasuredSkillRunner`
executes each case as a real task through the tool fabric and scores the result from the state
it leaves behind. The previous coverage scorer is gone — it compared a procedure's prose to the
case description, which let a procedure that executes nothing outscore a baseline and be
promoted.
"""

from __future__ import annotations

from typing import Any

from cogos.evaluation.skill_runner import MeasuredSkillRunner


def evaluate_candidate(runtime: Any, mission_id: str, candidate_id: str | None = None) -> dict[str, Any]:
    state = runtime.store.load_mission(mission_id)
    if state is None:
        return {"error": f"unknown mission {mission_id}"}
    cands = [c for c in state.candidate_skills if candidate_id is None or c.id == candidate_id]
    if not cands:
        return {"error": "no candidate skills"}
    cand = cands[0]
    report = runtime.skills.evaluate(cand, MeasuredSkillRunner(runtime.fabric))
    if report.promoted:
        doc = runtime.skills.promote(cand, report)
        outcome = {"promoted": True, "skill": doc.name}
    else:
        runtime.skills.reject(cand, report)
        outcome = {"promoted": False}
    runtime.store.save_mission(state, "skill_evaluated", {"candidate": cand.id, "promoted": report.promoted})
    return {"candidate": cand.name, "report": report.model_dump(mode="json"), **outcome}
