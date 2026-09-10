"""Shared fixtures for evaluation scenarios: isolated runtimes, scripted specialists."""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

from cogos.adapters.scripted import ScriptedExecutive
from cogos.config import CogosConfig, ExecutiveConfig, GovernanceConfig, MemoryConfig
from cogos.evaluation.demo import DEMO_IMPLEMENTATION, REQUIREMENTS, make_demo_workspace
from cogos.runtime import Runtime

BUGGY_IMPLEMENTATION = {
    "calc.py": "def add_percent(value: float, percent: float) -> float:\n    return round(value * (1 + percent), 2)  # BUG: percent not divided by 100\n",
    "test_calc.py": DEMO_IMPLEMENTATION["test_calc.py"],
}


class Sandbox:
    """An isolated workspace + runtime for one scenario."""

    def __init__(self, name: str, *, adapter: Optional[ScriptedExecutive] = None, governance: Optional[GovernanceConfig] = None, with_demo_project: bool = False, home: Optional[Path] = None):
        self.root = Path(tempfile.mkdtemp(prefix=f"cogos-eval-{name}-"))
        if with_demo_project:
            make_demo_workspace(self.root)
        self.home = home or (self.root / ".cogos")
        self.adapter = adapter or ScriptedExecutive()
        self.config = CogosConfig(home=self.home, repo_root=self.root, executive=ExecutiveConfig(adapter="scripted", model="claude-fable-5-1"), governance=governance or GovernanceConfig(), memory=MemoryConfig(consolidation_interval_cycles=5))
        self.runtime = Runtime(self.config, adapter=self.adapter)
        self.runtime.executive._sleep = lambda _s: None  # no real backoff in evals

    def reopen(self, adapter: Optional[ScriptedExecutive] = None) -> Runtime:
        """Simulate a fresh context: new runtime over the same durable store."""
        self.runtime.close()
        self.adapter = adapter or ScriptedExecutive()
        self.runtime = Runtime(self.config, adapter=self.adapter)
        self.runtime.executive._sleep = lambda _s: None
        return self.runtime

    def context(self, has_requirements: bool = True) -> dict[str, Any]:
        return {"root": str(self.root), "files": sorted(p.name for p in self.root.iterdir()), "has_requirements": has_requirements, "requirements_path": "REQUIREMENTS.md", "requirements_text": REQUIREMENTS if has_requirements else "", "test_command": f"{sys.executable} -m pytest -q"}

    def cleanup(self) -> None:
        try:
            self.runtime.close()
        finally:
            shutil.rmtree(self.root, ignore_errors=True)


def engineer_policy(root: Path, implementations: Optional[list[dict[str, str]]] = None, roles: tuple[str, ...] = ("engineer", "debugger")) -> Callable[[Any], Optional[dict[str, Any]]]:
    """Deterministic specialist that writes files; successive calls use successive implementations."""
    impls = list(implementations or [DEMO_IMPLEMENTATION])
    calls = {"n": 0}

    def policy(req: Any) -> Optional[dict[str, Any]]:
        spec = req.metadata.get("spec") or {}
        if spec.get("role") not in roles:
            return None
        impl = impls[min(calls["n"], len(impls) - 1)]
        calls["n"] += 1
        for name, content in impl.items():
            (root / name).write_text(content, encoding="utf-8")
        return specialist_report(spec.get("role", "engineer"), f"Implemented {', '.join(impl)} per requirements", findings=[("Implementation written to " + ", ".join(impl), "observation", 0.9, [("files written", "specialist:" + spec.get("role", "engineer"), "primary")])], artifacts=list(impl))

    return policy


def specialist_report(role: str, conclusion: str, *, confidence: float = 0.8, findings: Optional[list[tuple[str, str, float, list[tuple[str, str, str]]]]] = None, artifacts: Optional[list[str]] = None, unresolved: Optional[list[str]] = None, blocked: bool = False, blocked_reason: str = "", evidence_extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    fs = []
    for statement, status, conf, evs in findings or []:
        fs.append({"statement": statement, "epistemic_status": status, "confidence": conf, "evidence": [{"summary": s, "source": src, "kind": kind, "reliability": 0.8, **(evidence_extra or {})} for s, src, kind in evs]})
    return {"role": role, "conclusion": conclusion, "confidence": confidence, "findings": fs, "artifacts": artifacts or [], "unresolved": unresolved or [], "assumptions": [], "blocked": blocked, "blocked_reason": blocked_reason, "raw_notes": ""}


def researcher_policy(topic_findings: dict[str, list[tuple[str, str, float, list[tuple[str, str, str]]]]], default_conf: float = 0.8) -> Callable[[Any], Optional[dict[str, Any]]]:
    """Scripted researcher: answers each unknown with pre-defined sourced findings keyed by a keyword."""

    def policy(req: Any) -> Optional[dict[str, Any]]:
        spec = req.metadata.get("spec") or {}
        if spec.get("role") not in ("researcher", "market_analyst", "source_auditor", "skeptic", "analyst"):
            return None
        objective = str(spec.get("objective", "")).lower()
        for key, findings in topic_findings.items():
            if key in objective:
                return specialist_report(spec["role"], f"Findings on: {key}", confidence=default_conf, findings=findings)
        if "independent primary sources" in objective or "strengthen" in objective or "resolve contradictions" in objective:
            # Corroborate whichever findings the objective mentions with a second, independent primary source.
            matched = []
            for key, findings in topic_findings.items():
                for statement, status, conf, evs in findings:
                    if any(tok in objective for tok in statement.lower().split()[:4]):
                        matched.append((statement, status, conf, [(f"Corroborating primary source for: {statement[:60]}", f"https://independent-{key.replace(' ', '-')}.example/data", "primary")]))
            if matched:
                return specialist_report(spec["role"], "Corroborated with independent primary sources", confidence=default_conf, findings=matched)
        return specialist_report(spec["role"], "No specific findings available for this objective", confidence=0.3, unresolved=[str(spec.get("objective", ""))[:120]])

    return policy


def count_traces(runtime: Runtime, mission_id: str, kind: str, contains: str = "") -> int:
    return sum(1 for t in runtime.store.traces(mission_id, limit=5000) if t.kind == kind and (contains in t.summary))
