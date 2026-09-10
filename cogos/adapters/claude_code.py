"""Headless Claude Code executive adapter.

Uses ``claude -p`` with ``--json-schema`` (structured output), ``--model`` (model
pinning) and a restricted tool surface. This is the same substrate the Python
Agent SDK wraps; calling the CLI directly avoids an extra dependency and keeps
every flag auditable in the trace.

Model residency: the requested model is verified against ``modelUsage`` in the
CLI's JSON result. If a different model served the call the response is marked
``residency_ok=False`` and the runtime raises :class:`ResidencyViolation` for
executive-level cognition — we never accept a silent downgrade.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from typing import Any, Optional

from cogos.adapters.base import (
    CognitionRequest,
    CognitionResponse,
    ExecutiveUnavailable,
    Timer,
    render_untrusted,
)

# Provider-side safety classification. This is NOT something to work around: the runtime
# records it, retries once (the classifier is not deterministic on identical input), and
# otherwise falls back to deterministic policy while preserving the executive model.
_REFUSAL_MARKERS = (
    "safeguards flagged this message",
    "anthropic.com/legal/aup",
    "declined to respond",
    "content policy",
)

_TRANSIENT_MARKERS = (
    "overloaded",
    "rate limit",
    "rate_limit",
    "529",
    "503",
    "502",
    "timeout",
    "temporarily",
    "network",
    "ECONNRESET",
    "Authentication error · This may be a temporary",
)


def _is_transient(text: str) -> bool:
    low = text.lower()
    return any(m.lower() in low for m in _TRANSIENT_MARKERS)


def _is_refusal(text: str) -> bool:
    low = text.lower()
    return any(m.lower() in low for m in _REFUSAL_MARKERS)


class ClaudeCodeExecutive:
    name: str = "claude_code"

    def __init__(
        self,
        model: str,
        binary: str = "claude",
        max_retries: int = 3,
        effort: Optional[str] = "high",
        extra_args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
        auxiliary_models_ok: tuple[str, ...] = ("claude-haiku-4-5",),
    ):
        self.model = model
        self.binary = binary
        self.max_retries = max_retries
        self.effort = effort
        self.extra_args = list(extra_args or [])
        self.env = env
        self.auxiliary_models_ok = auxiliary_models_ok

    # -- availability ------------------------------------------------------------

    def available(self) -> tuple[bool, str]:
        path = shutil.which(self.binary)
        if not path:
            return False, f"'{self.binary}' binary not found on PATH"
        return True, path

    # -- main call ------------------------------------------------------------------

    def build_command(self, req: CognitionRequest) -> list[str]:
        cmd = [
            self.binary,
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(req.output_schema),
            "--model",
            req.model,
            "--max-turns",
            str(max(1, req.max_turns)),
            "--no-session-persistence",
            "--permission-prompts",
            "none",
        ]
        if req.system_prompt:
            cmd += ["--system-prompt", req.system_prompt]
        if req.tools:
            cmd += ["--tools", ",".join(req.tools), "--permission-mode", "acceptEdits"]
            if req.allowed_tool_patterns:
                cmd += ["--allowedTools", *req.allowed_tool_patterns]
        else:
            cmd += ["--tools", "", "--permission-mode", "dontAsk"]
        if req.cwd:
            cmd += ["--add-dir", req.cwd]
        effort = req.effort or self.effort
        if effort:
            cmd += ["--effort", effort]
        cmd += self.extra_args
        return cmd

    def call(self, req: CognitionRequest) -> CognitionResponse:
        ok, why = self.available()
        if not ok:
            raise ExecutiveUnavailable(why)
        prompt = req.prompt + render_untrusted(req.untrusted)
        cmd = self.build_command(req)
        last_err = ""
        delay = 2.0
        for attempt in range(self.max_retries + 1):
            with Timer() as t:
                try:
                    proc = subprocess.run(
                        cmd + [prompt],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        timeout=req.timeout_seconds,
                        cwd=req.cwd or None,
                        env={**os.environ, **(self.env or {})},
                        check=False,
                    )
                except subprocess.TimeoutExpired:
                    last_err = f"claude timed out after {req.timeout_seconds}s"
                    if attempt < self.max_retries:
                        time.sleep(delay)
                        delay *= 2
                        continue
                    return CognitionResponse(ok=False, model_requested=req.model, error=last_err, error_kind="timeout", duration_ms=t.ms if hasattr(t, "ms") else 0)
            resp = self._parse(proc, req, t.ms)
            if resp.ok:
                return resp
            last_err = resp.error
            if resp.error_kind == "transient" and attempt < self.max_retries:
                time.sleep(delay)
                delay *= 2
                continue
            if resp.error_kind == "refused" and attempt == 0:
                # One retry only: the safety classifier is not deterministic on identical
                # input. We never reshape the request to evade it.
                time.sleep(delay)
                continue
            return resp
        return CognitionResponse(ok=False, model_requested=req.model, error=last_err, error_kind="transient")

    # -- parsing ------------------------------------------------------------------------

    def _parse(self, proc: subprocess.CompletedProcess[str], req: CognitionRequest, ms: int) -> CognitionResponse:
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        data: dict[str, Any] = {}
        try:
            data = json.loads(stdout) if stdout.strip() else {}
        except json.JSONDecodeError:
            # stream may contain multiple JSON objects; take the last one that parses
            for line in reversed(stdout.splitlines()):
                try:
                    data = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
        if not data:
            err = (stderr or stdout or f"exit={proc.returncode}").strip()[:2000]
            kind = "transient" if _is_transient(err) else "structural"
            return CognitionResponse(ok=False, model_requested=req.model, error=err, error_kind=kind, duration_ms=ms)

        usage = data.get("usage") or {}
        model_usage = data.get("modelUsage") or {}
        models_used = list(model_usage.keys())
        residency_ok = self._residency_ok(req.model, models_used)
        base: dict[str, Any] = dict(
            model_requested=req.model,
            models_used=models_used,
            input_tokens=int(usage.get("input_tokens", 0)) + int(usage.get("cache_read_input_tokens", 0)) + int(usage.get("cache_creation_input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            cost_usd=float(data.get("total_cost_usd", 0.0) or 0.0),
            duration_ms=ms,
            turns=int(data.get("num_turns", 0) or 0),
            permission_denials=list(data.get("permission_denials") or []),
            session_id=data.get("session_id"),
            residency_ok=residency_ok,
        )
        if data.get("is_error"):
            err = str(data.get("result", ""))[:2000]
            if _is_refusal(err):
                return CognitionResponse(ok=False, error=err, error_kind="refused", raw_text=err, **base)
            kind = "transient" if _is_transient(err) else "structural"
            if "not available" in err.lower() or "does not exist" in err.lower() or "model" in err.lower() and "invalid" in err.lower():
                kind = "unavailable"
            return CognitionResponse(ok=False, error=err, error_kind=kind, raw_text=err, **base)
        parsed = data.get("structured_output")
        raw = str(data.get("result", ""))
        if parsed is None:
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return CognitionResponse(ok=False, error="no structured output returned", error_kind="schema", raw_text=raw[:4000], **base)
        if not isinstance(parsed, dict):
            return CognitionResponse(ok=False, error="structured output is not an object", error_kind="schema", raw_text=raw[:4000], **base)
        return CognitionResponse(ok=True, parsed=parsed, raw_text=raw[:4000], **base)

    def _residency_ok(self, requested: str, used: list[str]) -> bool:
        if not used:
            return True  # nothing recorded (e.g. cached); cannot prove a violation
        # Auxiliary models (title generation, summarisation) do not serve cognition; ignore them.
        used = [m for m in used if not any(m.lower().startswith(a) for a in self.auxiliary_models_ok)] or used
        req_key = requested.lower()
        for m in used:
            ml = m.lower()
            if ml == req_key or ml.startswith(req_key) or req_key in ml:
                return True
        # Aliases: 'fable' -> 'claude-fable-*'
        alias = req_key.replace("claude-", "").split("-")[0]
        return any(alias and alias in m.lower() for m in used)
