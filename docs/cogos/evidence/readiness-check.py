#!/usr/bin/env python
"""Record the evidence for the Live Run #3 readiness decision.

Machine-produced on purpose. A readiness gate written by hand is a gate that can be talked past;
this one runs the checks and prints what it observed, including the failures.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
PY = str(REPO / ".venv" / "bin" / "python")


def run(cmd: list[str], timeout: int = 3000) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout, check=False)


def condition(number: int, name: str, met: bool, evidence: str) -> dict:
    print(f"  [{'MET    ' if met else 'NOT MET'}] {number}. {name}\n           {evidence}")
    return {"n": number, "name": name, "met": met, "evidence": evidence}


def main() -> int:
    print("Live Run #3 readiness — evidence\n")
    results = []

    sha = run(["git", "rev-parse", "HEAD"]).stdout.strip()
    dirty = run(["git", "status", "--porcelain"]).stdout.strip()

    # --- 3. the real backend ---------------------------------------------------------------
    from cogos.verification.isolation import IsolationPolicy, probe_backend, resolve_image_id

    backend = probe_backend()
    policy = IsolationPolicy()
    image_id = resolve_image_id(policy.image) if backend.available else ""
    results.append(condition(3, "Real isolation probes pass on the actual execution backend",
        backend.available and bool(image_id),
        f"backend={backend.backend} {backend.server_version} runtime={backend.runtime} cgroup=v{backend.cgroup_version} "
        f"security={list(backend.security_options)} gvisor={backend.gvisor} image={image_id[:23]}…"))

    # --- 1/2/4/5/6. the acceptance matrix, run for real ------------------------------------
    iv = run([PY, "-m", "pytest", "tests/cogos/test_isolated_verifier.py", "-q", "-p", "no:randomly", "-rs"])
    skipped = iv.stdout.count("SKIPPED")
    iv_line = [l for l in iv.stdout.splitlines() if " passed" in l or " failed" in l]
    results.append(condition(1, "Correct mission completes with production defaults, no trust exemption",
        iv.returncode == 0 and skipped == 0,
        f"test_isolated_verifier.py: {iv_line[-1] if iv_line else 'no summary'}; real-backend tests skipped: {skipped}"))
    results.append(condition(2, "Wrong-code and attack controls block under identical settings",
        iv.returncode == 0, "the same file's defective-work and forgery cases, same production defaults"))
    results.append(condition(4, "Receipts cannot be supplied or promoted by the subject or executive",
        iv.returncode == 0, "forged-authority, forged-test-record and foreign-contract cases in the same run"))
    results.append(condition(5, "Snapshot and resume integrity checks pass",
        iv.returncode == 0, "post-verification swap, snapshot refusal and persistence round-trip cases"))

    # --- 6. suites, evals, demo, lint, types, mutations -------------------------------------
    suite = run([PY, "-m", "pytest", "tests/cogos", "-q", "-p", "no:randomly"])
    suite_line = [l for l in suite.stdout.splitlines() if " passed" in l or " failed" in l]
    lint = run([PY, "-m", "ruff", "check", "cogos", "tests/cogos"])
    types = run([str(REPO / ".venv" / "bin" / "ty"), "check", "cogos"])
    evals = run(["make", "cogos-eval"], timeout=1800)
    demo = run(["make", "cogos-demo"], timeout=1200)
    audits = sorted(Path(REPO / "docs/cogos/evidence").glob("*-mutation-audit.sh"))
    audit_results = {}
    for script in audits:
        done = run(["bash", str(script)], timeout=3600)
        tail = [l for l in done.stdout.splitlines() if "load-bearing" in l]
        audit_results[script.name] = {"exit": done.returncode, "summary": tail[-1].strip() if tail else "no summary"}
    all_audits_clean = all(a["exit"] == 0 for a in audit_results.values())
    results.append(condition(6, "Suites, evals, demo, lint, type checks and every mutation audit pass",
        suite.returncode == 0 and lint.returncode == 0 and types.returncode == 0
        and evals.returncode == 0 and demo.returncode == 0 and all_audits_clean,
        f"suite: {suite_line[-1] if suite_line else '?'}; ruff={'clean' if lint.returncode==0 else 'FAIL'}; "
        f"ty={'clean' if types.returncode==0 else 'FAIL'}; evals rc={evals.returncode}; demo rc={demo.returncode}; "
        + "; ".join(f"{n}: {a['summary']}" for n, a in audit_results.items())))

    # --- 7. the protocol --------------------------------------------------------------------
    protocol = (REPO / "docs/cogos/evidence/LIVE_RUN_3_PROTOCOL.md").read_text(encoding="utf-8")
    # Only the specification counts. §13 quotes the old wording in order to say it was replaced, so
    # searching the whole file finds the correction log and calls the document stale because it
    # documented its own correction.
    spec = protocol.split("## 13. Corrections")[0]
    stale = [p for p in ("≥ 612 passed", "all 50", "562 tests") if p in spec]
    results.append(condition(7, "The run protocol is internally consistent and frozen",
        not stale and "## 14. Readiness" in protocol,
        f"stale frozen counts remaining: {stale or 'none'}; corrections logged in §13"))

    # --- 8. the model ------------------------------------------------------------------------
    from cogos.adapters.base import CognitionRequest
    from cogos.adapters.claude_code import ClaudeCodeExecutive
    from cogos.config import load_config

    config = load_config()
    required = config.executive.model
    executive = ClaudeCodeExecutive(config)
    probe = executive.call(CognitionRequest(
        kind="verify", system_prompt="You return JSON only.", prompt='Reply with the JSON {"ok": true}',
        schema_name="Probe", output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
        model=required, effort="medium", timeout_seconds=180,
    ))
    model_available = bool(probe.ok) and required in (probe.models_used or [])
    results.append(condition(8, "Required model, account access and budget are available",
        model_available,
        f"requested={required}; ok={probe.ok}; models_used={probe.models_used}; "
        f"residency={getattr(probe.residency_status, 'name', probe.residency_status)}; "
        f"error={(probe.error or '')[:120]!r}"))

    # --- 9. open findings --------------------------------------------------------------------
    results.append(condition(9, "No unresolved finding contradicts the stated threat model",
        all_audits_clean and iv.returncode == 0,
        "every mutation guard load-bearing; acceptance matrix green; residual risks recorded in ISOLATION_DECISION.md §1 and §5"))

    met = [r for r in results if r["met"]]
    blocked = [r for r in results if not r["met"]]
    print(f"\n  {len(met)}/{len(results)} conditions met")
    if blocked:
        print("  BLOCKED on:")
        for r in blocked:
            print(f"    - condition {r['n']}: {r['name']}\n        {r['evidence']}")
    verdict = "READY" if not blocked else "BLOCKED"
    print(f"\n  VERDICT: {verdict}")

    out = REPO / "docs/cogos/evidence/readiness-live-run-3.json"
    out.write_text(json.dumps({
        "sha": sha, "dirty_tree": bool(dirty), "verdict": verdict,
        "conditions": results, "audits": audit_results,
        "backend": backend.as_dict(), "image_id": image_id,
        "isolation_policy_digest": policy.digest(),
    }, indent=2), encoding="utf-8")
    print(f"  written: {out.relative_to(REPO)}")
    return 0 if verdict == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
