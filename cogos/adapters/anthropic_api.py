"""Direct Anthropic Messages API executive adapter (optional).

Requires the ``anthropic`` extra (already pinned in this repository's
``pyproject.toml``) and ``ANTHROPIC_API_KEY`` in the environment. It supports
reasoning-only cognition calls (no tools); specialists that need tools should
use :mod:`cogos.adapters.claude_code`.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from cogos.adapters.base import CognitionRequest, CognitionResponse, ExecutiveUnavailable, Timer, render_untrusted


class AnthropicApiExecutive:
    name: str = "anthropic_api"

    def __init__(self, model: str, max_retries: int = 3, max_tokens: int = 8000, client: Optional[Any] = None):
        self.model = model
        self.max_retries = max_retries
        self.max_tokens = max_tokens
        self._client = client

    def _client_or_raise(self) -> Any:
        if self._client is not None:
            return self._client
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ExecutiveUnavailable("ANTHROPIC_API_KEY is not set")
        try:
            import anthropic  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise ExecutiveUnavailable("anthropic package not installed (pip install 'hermes-agent[anthropic]')") from exc
        self._client = anthropic.Anthropic()
        return self._client

    def call(self, req: CognitionRequest) -> CognitionResponse:
        client = self._client_or_raise()
        prompt = req.prompt + render_untrusted(req.untrusted)
        delay = 2.0
        last_err = ""
        for attempt in range(self.max_retries + 1):
            with Timer() as t:
                try:
                    msg = client.messages.create(
                        model=req.model,
                        max_tokens=self.max_tokens,
                        system=req.system_prompt,
                        messages=[{"role": "user", "content": prompt}],
                        output_config={"format": {"type": "json_schema", "schema": req.output_schema}},
                    )
                except Exception as exc:  # noqa: BLE001 - provider errors are heterogeneous
                    last_err = str(exc)[:2000]
                    kind = "transient" if any(k in last_err.lower() for k in ("overloaded", "rate", "529", "503", "timeout")) else "structural"
                    if kind == "transient" and attempt < self.max_retries:
                        time.sleep(delay)
                        delay *= 2
                        continue
                    return CognitionResponse(ok=False, model_requested=req.model, error=last_err, error_kind=kind)
            text = "".join(getattr(b, "text", "") for b in getattr(msg, "content", []) if getattr(b, "type", "") == "text")
            usage = getattr(msg, "usage", None)
            base: dict[str, Any] = dict(
                model_requested=req.model,
                models_used=[getattr(msg, "model", req.model)],
                input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                duration_ms=t.ms,
                turns=1,
                residency_ok=str(getattr(msg, "model", req.model)).startswith(req.model.split("-2")[0]),
            )
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return CognitionResponse(ok=False, error="non-JSON structured output", error_kind="schema", raw_text=text[:4000], **base)
            return CognitionResponse(ok=True, parsed=parsed, raw_text=text[:4000], **base)
        return CognitionResponse(ok=False, model_requested=req.model, error=last_err, error_kind="transient")
