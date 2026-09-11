"""Executive-model adapters.

The runtime talks to the resident executive model exclusively through
:class:`ExecutiveModel`. Adapters:

* :mod:`cogos.adapters.claude_code` — headless ``claude -p`` with structured
  output and model pinning (the Agent SDK is a wrapper over this CLI).
* :mod:`cogos.adapters.anthropic_api` — direct Messages API (lazy import).
* :mod:`cogos.adapters.scripted` — deterministic policies for tests/evals.
"""

from cogos.adapters.base import (  # noqa: F401
    CognitionRequest,
    CognitionResponse,
    ExecutiveModel,
    ExecutiveUnavailable,
    ResidencyViolation,
    UntrustedBlock,
)


def build_adapter(name: str, **kwargs):
    if name == "claude_code":
        from cogos.adapters.claude_code import ClaudeCodeExecutive

        return ClaudeCodeExecutive(**kwargs)
    if name == "anthropic_api":
        from cogos.adapters.anthropic_api import AnthropicApiExecutive

        return AnthropicApiExecutive(**kwargs)
    if name == "scripted":
        from cogos.adapters.scripted import HeuristicExecutive

        return HeuristicExecutive(**kwargs)
    raise ValueError(f"unknown executive adapter: {name}")
