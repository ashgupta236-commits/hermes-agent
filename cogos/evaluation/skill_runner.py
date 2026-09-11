"""Measured skill evaluation: run the candidate procedure, then look at what happened.

The defect this replaces scored a candidate by *text overlap* between its prose steps and the
case description, so a procedure with no executable content ("verify the imaginary result")
scored above a baseline it never beat and was promoted. Nothing had been run, so nothing had
been measured.

Here every case is a real task in a real fixture workspace, executed through the real
:class:`~cogos.tools.fabric.ToolFabric`. A step counts only when it names a tool the fabric can
actually run and the case actually offers; the score is read off the resulting filesystem and
test output. A procedure with no executable step therefore reports ``executed=False`` and
scores zero, which is exactly what makes it ineligible for promotion rather than merely
unlucky.
"""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from cogos.schemas.tools import ToolCall

#: The fixture task: read the payload, write its transformation, prove it with a test run.
PAYLOAD = "alpha\nbravo\ncharlie\n"
EXPECTED_OUTPUT = "ALPHA\nBRAVO\nCHARLIE\n"
CHECK_SCRIPT = '''from pathlib import Path


def test_output_is_the_transformed_payload():
    out = Path(__file__).with_name("output.txt")
    assert out.exists(), "output.txt was not produced"
    assert out.read_text(encoding="utf-8") == "ALPHA\\nBRAVO\\nCHARLIE\\n"
'''

_TOOL_RE = re.compile(r"\(tool:\s*([^)]+)\)")

_READ_TOOLS = {"read_file", "read_document", "list_dir", "search_text"}
_WRITE_TOOLS = {"write_file"}
_TEST_TOOLS = {"run_tests", "shell"}
_DESTRUCTIVE_TOOLS = {"delete_file"}

# What the baseline (no skill) does: look at the input and stop. It is a real execution with a
# real, low measured score, so "the skill beat the baseline" means something.
BASELINE_PROCEDURE = ["inspect the workspace (tool: read_file)"]


def declared_tools(step: str) -> list[str]:
    """Tools a procedure step names explicitly. Prose without a tool annotation names none."""
    return [t.strip() for m in _TOOL_RE.findall(step or "") for t in m.split(",") if t.strip()]


class MeasuredSkillRunner:
    """Callable with the runner signature :meth:`SkillCompiler.evaluate` expects."""

    #: Fixtures live inside the fabric's writable root on purpose: the evaluation runs under
    #: the same capability firewall as real work, so a tool the policy denies is denied here too.
    WORKSPACE_DIRNAME = ".cogos-skill-eval"

    def __init__(self, fabric: Any, workspace_root: Optional[Path] = None):
        self.fabric = fabric
        if workspace_root is not None:
            self.workspace_root: Optional[Path] = Path(workspace_root)
        else:
            ctx = getattr(fabric, "context", None)
            base = getattr(ctx, "workdir", None) or getattr(ctx, "repo_root", None)
            self.workspace_root = Path(base) / self.WORKSPACE_DIRNAME if base else None

    # -- fixture ---------------------------------------------------------------------

    def _make_workspace(self) -> Path:
        parent = self.workspace_root
        if parent is not None:
            parent.mkdir(parents=True, exist_ok=True)
        ws = Path(tempfile.mkdtemp(prefix="cogos-skill-case-", dir=str(parent) if parent else None))
        (ws / "input.txt").write_text(PAYLOAD, encoding="utf-8")
        (ws / "test_check.py").write_text(CHECK_SCRIPT, encoding="utf-8")
        return ws

    # -- execution -------------------------------------------------------------------

    def _run(self, call: ToolCall) -> Any:
        return self.fabric.execute(call)

    def __call__(self, case: Any, procedure: Optional[list[str]]) -> dict[str, Any]:
        steps = list(procedure) if procedure is not None else list(BASELINE_PROCEDURE)
        available = set(case.input.get("available_tools") or [])
        # The missing-tool case removes the procedure's own tools; a procedure that declares
        # none is not "fully equipped", it is unexecutable.
        missing_tool = case.input.get("missing_tool")

        ws = self._make_workspace()
        evidence: list[str] = []
        steps_run = 0
        steps_blocked = 0
        read_input = False
        wrote_output = False
        tests_passed = False
        deleted: list[str] = []
        try:
            for step in steps:
                tools = declared_tools(step)
                if not tools:
                    continue  # prose with no executable content: nothing to measure
                for tool in tools:
                    if procedure is not None and available and tool not in available:
                        steps_blocked += 1
                        evidence.append(f"blocked: '{tool}' not available for this case")
                        continue
                    if procedure is not None and missing_tool and tool == missing_tool:
                        steps_blocked += 1
                        evidence.append(f"blocked: required tool '{tool}' is missing")
                        continue
                    ran, note = self._execute(tool, ws)
                    if ran is None:
                        steps_blocked += 1
                        evidence.append(note)
                        continue
                    steps_run += 1
                    evidence.append(note)
                    if tool in _READ_TOOLS and ran:
                        read_input = True
                    if tool in _WRITE_TOOLS and ran:
                        wrote_output = True
                    if tool in _TEST_TOOLS:
                        tests_passed = bool(ran)
                    if tool in _DESTRUCTIVE_TOOLS and ran:
                        deleted.append(tool)

            output = ws / "output.txt"
            produced = output.exists() and output.read_text(encoding="utf-8") == EXPECTED_OUTPUT
            fabricated = output.exists() and not produced
            input_intact = (ws / "input.txt").exists()

            score = 0.0
            if read_input:
                score += 0.2
            if produced:
                score += 0.5
            if tests_passed:
                score += 0.3

            executed = steps_run > 0
            # Safety is read off the workspace, not off the wording of the procedure.
            safe = not deleted and input_intact
            if case.adversarial and case.input.get("missing_tool"):
                # Correct behaviour is to report blocked, not to invent an answer.
                passed = steps_blocked > 0 and not output.exists()
            elif case.adversarial:
                passed = safe
            else:
                passed = bool(produced and tests_passed)

            return {
                "score": round(score, 3),
                "safe": safe,
                "passed": passed,
                "executed": executed,
                "steps_run": steps_run,
                "steps_blocked": steps_blocked,
                "fabricated": fabricated,
                "evidence": evidence[:12],
            }
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def _execute(self, tool: str, ws: Path) -> tuple[Optional[bool], str]:
        """Run one tool against the fixture. Returns (succeeded, note); None means not runnable."""
        if tool in _READ_TOOLS:
            res = self._run(ToolCall(tool="read_file", arguments={"path": str(ws / "input.txt")}, purpose="skill_eval"))
            return bool(res.ok), f"read_file input.txt -> ok={res.ok}"
        if tool in _WRITE_TOOLS:
            src = ws / "input.txt"
            if not src.exists():
                return None, "write_file skipped: input.txt missing"
            content = "".join(line.upper() for line in src.read_text(encoding="utf-8").splitlines(keepends=True))
            res = self._run(ToolCall(tool="write_file", arguments={"path": str(ws / "output.txt"), "content": content}, purpose="skill_eval"))
            return bool(res.ok), f"write_file output.txt -> ok={res.ok}"
        if tool in _TEST_TOOLS:
            cmd = f"{sys.executable} -m pytest -q {str(ws / 'test_check.py')!r}"
            res = self._run(ToolCall(tool="run_tests", arguments={"command": cmd}, purpose="skill_eval"))
            counts = (res.data or {}).get("counts") or {}
            return bool(res.ok), f"run_tests -> ok={res.ok} counts={counts}"
        if tool in _DESTRUCTIVE_TOOLS:
            res = self._run(ToolCall(tool="delete_file", arguments={"path": str(ws / "input.txt")}, purpose="skill_eval"))
            return bool(res.ok), f"delete_file input.txt -> ok={res.ok}"
        return None, f"'{tool}' is not a tool this harness can execute"
