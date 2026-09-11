"""Runtime configuration.

Behavioural settings live in ``cogos.yaml`` (repo root or ``COGOS_HOME``);
secrets stay in the environment. Nothing here is required: every setting has a
professional default so ``python -m cogos`` works in a fresh checkout.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field

DEFAULT_EXECUTIVE_MODEL = "claude-fable-5-1"
"""Resident executive model. Overridden only by explicit configuration."""


class BudgetConfig(BaseModel):
    max_cycles: int = 200
    max_model_calls: int = 400
    max_subagents: int = 20
    max_cost_usd: Optional[float] = None
    max_wall_clock_seconds: Optional[float] = None


class GovernanceConfig(BaseModel):
    """Capability firewall policy. Defaults maximise autonomy for reversible local work."""

    allow_network: bool = True
    allowed_domains: list[str] = Field(default_factory=list, description="Empty = any domain (subject to sandbox)")
    denied_domains: list[str] = Field(default_factory=list)
    allow_shell: bool = True
    shell_timeout_seconds: int = 120
    writable_roots: list[str] = Field(default_factory=list, description="Empty = repository root only")
    always_require_human: list[str] = Field(
        default_factory=lambda: [
            "destructive",
            "financial",
            "legally_significant",
            "credential_sensitive",
        ]
    )
    denied_action_classes: list[str] = Field(default_factory=list)
    max_output_chars: int = 20_000
    #: Whether the subject workspace's code may be treated as non-adversarial *towards the
    #: verifier*. Default False, and the default is the honest one.
    #:
    #: A test run imports the code under test into the process that reports on it. That process
    #: can rewrite pytest's internals, unregister the report plugin, mutate result objects, read
    #: the report path out of its own argv and overwrite the file, and call `os._exit(0)`.
    #: Measured, not assumed: a `conftest.py` hookwrapper on `pytest_runtest_makereport` turns
    #: every failure into a pass and the *genuine* runner then writes a genuine, well-formed,
    #: correctly located, freshly stamped report whose contents are false. `--noconftest` does not
    #: change this, because the test modules are subject code too.
    #:
    #: So same-process verification of adversarial subject code is **not supported** by this
    #: runtime, and with this flag False the runtime fails closed: an in-process test run is
    #: capped at `process_observation`, which cannot close a behavioural criterion or pass the
    #: completion gate. Setting it True is a statement by a human that this particular workspace's
    #: code is not targeting the verifier — an authorization decision, not a tuning knob. It never
    #: disables the trusted harness or the differential control; it only permits their result to
    #: carry authority.
    trust_workspace_code: bool = False


class ExecutiveConfig(BaseModel):
    model: str = DEFAULT_EXECUTIVE_MODEL
    adapter: str = Field(default="claude_code", description="claude_code|anthropic_api|scripted")
    allow_cheaper_specialist_models: bool = Field(
        default=False, description="Specialists inherit the executive model unless explicitly authorised"
    )
    specialist_model: Optional[str] = None
    effort: str = Field(default="high", description="low|medium|high|xhigh|max")
    call_timeout_seconds: int = 900
    max_retries: int = 3
    claude_binary: str = "claude"
    extra_cli_args: list[str] = Field(default_factory=list)


class MemoryConfig(BaseModel):
    max_retrieval_items: int = 12
    min_importance_to_store: float = 0.35
    default_ttl_days: Optional[int] = None
    consolidation_interval_cycles: int = 10


class CogosConfig(BaseModel):
    home: Path = Field(default_factory=lambda: Path(os.environ.get("COGOS_HOME", ".cogos")))
    repo_root: Path = Field(default_factory=lambda: Path.cwd())
    executive: ExecutiveConfig = Field(default_factory=ExecutiveConfig)
    governance: GovernanceConfig = Field(default_factory=GovernanceConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    checkpoint_every_cycles: int = 1
    workspace_max_chars: int = 12_000
    trace_to_stdout: bool = False

    @property
    def db_path(self) -> Path:
        return self.home / "cogos.db"

    @property
    def snapshots_dir(self) -> Path:
        return self.home / "snapshots"

    @property
    def artifacts_dir(self) -> Path:
        return self.home / "artifacts"

    @property
    def skills_dir(self) -> Path:
        return self.home / "skills"

    def ensure_dirs(self) -> None:
        for d in (self.home, self.snapshots_dir, self.artifacts_dir, self.skills_dir):
            d.mkdir(parents=True, exist_ok=True)


def find_repo_root(start: Optional[Path] = None) -> Path:
    cur = (start or Path.cwd()).resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / ".git").exists():
            return candidate
    return cur


def load_config(path: Optional[Path] = None, overrides: Optional[dict[str, Any]] = None) -> CogosConfig:
    """Load config from ``cogos.yaml`` (if present) with environment/explicit overrides."""
    root = find_repo_root()
    data: dict[str, Any] = {}
    cfg_path = path or Path(os.environ.get("COGOS_CONFIG", root / "cogos.yaml"))
    if cfg_path.exists():
        with open(cfg_path, encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        if isinstance(loaded, dict):
            data.update(loaded)
    if overrides:
        _deep_update(data, overrides)
    data.setdefault("repo_root", str(root))
    if "home" not in data:
        data["home"] = os.environ.get("COGOS_HOME", str(root / ".cogos"))
    if os.environ.get("COGOS_EXECUTIVE_MODEL"):
        data.setdefault("executive", {})["model"] = os.environ["COGOS_EXECUTIVE_MODEL"]
    if os.environ.get("COGOS_ADAPTER"):
        data.setdefault("executive", {})["adapter"] = os.environ["COGOS_ADAPTER"]
    cfg = CogosConfig.model_validate(data)
    cfg.home = Path(cfg.home)
    if not cfg.home.is_absolute():
        cfg.home = Path(cfg.repo_root) / cfg.home
    return cfg


def _deep_update(base: dict[str, Any], upd: dict[str, Any]) -> None:
    for k, v in upd.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
