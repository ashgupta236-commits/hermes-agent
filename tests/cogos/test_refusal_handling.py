"""Provider safety classifications are recorded and degraded around, never worked around.

A refusal narrows what one cognition call can do. It must not change the resident
executive model, must not stop the mission, and must never be met by reshaping the
request to evade the safeguard.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from cogos.adapters.base import CognitionRequest
from cogos.adapters.claude_code import ClaudeCodeExecutive, _is_refusal
from cogos.evaluation.support import Sandbox, engineer_policy
from cogos.schemas.mission import MissionStatus

REFUSAL_TEXT = "API Error: Fable 5.1's safeguards flagged this message (https://www.anthropic.com/legal/aup). This sometimes happens with long prompts."


def _cli_result(**over: Any) -> str:
    base = {
        "is_error": True,
        "result": REFUSAL_TEXT,
        "usage": {"input_tokens": 10, "output_tokens": 0},
        "modelUsage": {"claude-fable-5-1": {}},
        "num_turns": 1,
        "total_cost_usd": 0.01,
        "session_id": "s1",
        "permission_denials": [],
    }
    base.update(over)
    return json.dumps(base)


def _request() -> CognitionRequest:
    return CognitionRequest(kind="select", system_prompt="sys", prompt="p", schema_name="X", output_schema={"type": "object"}, model="claude-fable-5-1")


@pytest.mark.parametrize(
    "text,expected",
    [
        (REFUSAL_TEXT, True),
        ("API Error: model declined to respond", True),
        ("Overloaded, please retry", False),
        ("exit=1", False),
    ],
)
def test_refusal_detection(text: str, expected: bool) -> None:
    assert _is_refusal(text) is expected


def test_refusal_is_its_own_error_kind_and_retries_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, _cli_result(), "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("cogos.adapters.claude_code.time.sleep", lambda _s: None)
    monkeypatch.setattr("cogos.adapters.claude_code.shutil.which", lambda _b: "/usr/bin/claude")

    resp = ClaudeCodeExecutive("claude-fable-5-1", max_retries=3).call(_request())
    assert resp.ok is False
    assert resp.error_kind == "refused"
    # exactly one retry: the classifier is non-deterministic, but we never reshape the request
    assert len(calls) == 2
    assert calls[0] == calls[1], "the retry must send an identical request, never a reshaped one"


def test_refusal_retry_can_succeed(monkeypatch: pytest.MonkeyPatch) -> None:
    outputs = [_cli_result(), json.dumps({"is_error": False, "structured_output": {"ok": True}, "result": "{}", "usage": {}, "modelUsage": {"claude-fable-5-1": {}}, "num_turns": 1})]

    def fake_run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, outputs.pop(0), "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("cogos.adapters.claude_code.time.sleep", lambda _s: None)
    monkeypatch.setattr("cogos.adapters.claude_code.shutil.which", lambda _b: "/usr/bin/claude")

    resp = ClaudeCodeExecutive("claude-fable-5-1").call(_request())
    assert resp.ok is True and resp.parsed == {"ok": True}


def test_mission_survives_refusals_with_residency_preserved() -> None:
    """Every select call is refused; the loop degrades to deterministic policy and finishes."""
    sb = Sandbox("refused", with_demo_project=True)
    sb.adapter.policies["specialist"] = engineer_policy(sb.root)
    sb.adapter.fail_kinds["select"] = "refused"
    try:
        state = sb.runtime.new_mission("Build the feature described in REQUIREMENTS.md.", context=sb.context())
        model_before = state.executive_model
        state = sb.runtime.run(state.mission_id, max_cycles=40)

        assert state.executive_model == model_before, "a refusal must never downgrade the executive"
        assert {r.model for r in sb.adapter.calls} == {model_before}
        assert any("declined by provider safety classification" in t.summary for t in sb.runtime.store.traces(state.mission_id, limit=2000))
        assert state.capability_state.get("cognition:select", {}).get("last_verdict") == "refused"
        assert any("provider safety classification" in n for n in state.notes)
        assert state.status is MissionStatus.COMPLETE, "unaffected work must continue to completion"
    finally:
        sb.cleanup()
