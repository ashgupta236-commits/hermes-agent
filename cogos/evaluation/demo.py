"""Minimal autonomous demonstration.

Creates an isolated workspace with a small project and a REQUIREMENTS.md, compiles
the mission "Build the feature described in REQUIREMENTS.md", and runs the
executive loop. With the default ``scripted`` adapter this runs fully offline
(specialists are replaced by a deterministic engineer that implements the
requirement); with ``claude_code`` the resident Claude model does the work.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

from cogos.config import CogosConfig, ExecutiveConfig

REQUIREMENTS = """# Requirements

Implement `calc.py` with a function `add_percent(value: float, percent: float) -> float`
that returns `value` increased by `percent` percent, rounded to 2 decimals.
Add tests in `test_calc.py` covering a positive, zero, and negative percent.
"""

DEMO_IMPLEMENTATION = {
    "calc.py": "def add_percent(value: float, percent: float) -> float:\n    return round(value * (1 + percent / 100.0), 2)\n",
    "test_calc.py": "from calc import add_percent\n\n\ndef test_positive():\n    assert add_percent(100, 15) == 115.0\n\n\ndef test_zero():\n    assert add_percent(50, 0) == 50.0\n\n\ndef test_negative():\n    assert add_percent(200, -50) == 100.0\n",
}


def make_demo_workspace(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "REQUIREMENTS.md").write_text(REQUIREMENTS, encoding="utf-8")
    (root / "README.md").write_text("# demo project\n", encoding="utf-8")
    (root / "pytest.ini").write_text("[pytest]\ntestpaths = .\n", encoding="utf-8")
    return root


def scripted_engineer(root: Path):
    """Deterministic stand-in for a specialist engineer (offline demo only)."""

    def policy(req):
        spec = req.metadata.get("spec") or {}
        if spec.get("role") not in ("engineer", "debugger"):
            return None
        for name, content in DEMO_IMPLEMENTATION.items():
            (root / name).write_text(content, encoding="utf-8")
        return {
            "role": spec.get("role"),
            "conclusion": "Implemented add_percent in calc.py with three tests in test_calc.py",
            "confidence": 0.85,
            "findings": [{"statement": "calc.add_percent implemented per REQUIREMENTS.md", "confidence": 0.9, "epistemic_status": "observation", "evidence": [{"summary": "files written: calc.py, test_calc.py", "source": "specialist:engineer", "kind": "primary", "reliability": 0.9}]}],
            "artifacts": ["calc.py", "test_calc.py"],
            "unresolved": [],
            "assumptions": ["rounding to 2 decimals uses Python round()"],
            "blocked": False,
            "blocked_reason": "",
            "raw_notes": "",
        }

    return policy


def run_demo(adapter: Optional[str] = None, objective: Optional[str] = None, max_cycles: int = 40, verbose: bool = False, home: Optional[str] = None, keep: bool = False) -> int:
    import sys

    from cogos.adapters.scripted import ScriptedExecutive
    from cogos.runtime import Runtime

    adapter_name = adapter or "scripted"
    workdir = Path(tempfile.mkdtemp(prefix="cogos-demo-"))
    make_demo_workspace(workdir)
    cfg = CogosConfig(home=Path(home) if home else workdir / ".cogos", repo_root=workdir, executive=ExecutiveConfig(adapter=adapter_name), trace_to_stdout=verbose)
    cfg.executive.adapter = adapter_name
    exec_adapter = ScriptedExecutive(policies={"specialist": scripted_engineer(workdir)}) if adapter_name == "scripted" else None
    rt = Runtime(cfg, adapter=exec_adapter, stdout_trace=verbose)
    obj = objective or "Build the feature described in REQUIREMENTS.md."
    context = {"root": str(workdir), "files": sorted(p.name for p in workdir.iterdir()), "has_requirements": True, "requirements_path": "REQUIREMENTS.md", "requirements_text": REQUIREMENTS, "test_command": f"{sys.executable} -m pytest -q"}
    state = rt.new_mission(obj, context=context)
    print(f"[demo] workspace={workdir} mission={state.mission_id} adapter={adapter_name} model={state.executive_model}")
    state = rt.run(state.mission_id, max_cycles=max_cycles)
    status = rt.status(state.mission_id)
    print(json.dumps({k: status[k] for k in ("status", "progress", "confidence", "criteria", "tasks", "usage", "synthesis", "notes")}, indent=1, default=str))
    print("\n[demo] trace timeline:")
    for line in rt.tracer.timeline(state.mission_id, limit=80):
        print("  " + line)
    ok = status["status"] == "complete"
    print(f"\n[demo] {'SUCCESS' if ok else 'NOT COMPLETE'}: mission status={status['status']}")
    rt.close()
    if not keep:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0 if ok else 1


def demo_metadata() -> dict[str, Any]:
    return {"requirements": REQUIREMENTS, "files": list(DEMO_IMPLEMENTATION)}
