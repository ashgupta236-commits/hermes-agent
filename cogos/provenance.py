"""Run provenance: which implementation produced this trace (live-run area I).

During the live incident the mission kept running while fixes were committed to the repository,
so `git HEAD` and the code actually loaded in the process diverged. A trace that cannot say which
build produced it cannot be reproduced, and a fix cannot be credited or blamed by looking at it.

Everything here is cheap, dependency-free and best-effort: provenance capture must never be able
to fail a mission. Where a fact cannot be established it is recorded as unknown rather than
guessed.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from cogos.ids import iso_now


class RunProvenance(BaseModel):
    """Enough to identify the implementation and environment behind a mission's trace."""

    captured_at: str = Field(default_factory=iso_now)
    git_commit: str = ""
    git_branch: str = ""
    git_dirty: Optional[bool] = Field(default=None, description="None when it could not be determined")
    dirty_files: list[str] = Field(default_factory=list)
    adapter: str = ""
    adapter_version: str = ""
    model_requested: str = ""
    config_hash: str = ""
    mission_schema_version: int = 0
    python_version: str = ""
    platform: str = ""
    workspace: str = Field(default="", description="The directory the mission operates on, when it differs from the implementation")
    workspace_commit: str = ""

    def describe(self) -> str:
        state = "dirty" if self.git_dirty else ("clean" if self.git_dirty is False else "unknown")
        return f"{self.git_commit[:12] or 'unknown'} ({state}) via {self.adapter} {self.adapter_version}".strip()


def _git(repo_root: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(repo_root), capture_output=True, text=True, timeout=10, check=False
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _adapter_version(adapter: Any) -> str:
    """Best-effort version of whatever is actually serving cognition."""
    binary = getattr(adapter, "binary", None)
    if not binary:
        return ""
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10, check=False)
        return proc.stdout.strip().splitlines()[0][:120] if proc.returncode == 0 and proc.stdout.strip() else ""
    except (OSError, subprocess.SubprocessError, IndexError):
        return ""


def capture(config: Any, adapter: Any, schema_version: int = 0) -> RunProvenance:
    """Record what is running, now. Never raises."""
    prov = RunProvenance(mission_schema_version=schema_version)
    try:
        # The commit that matters is the one the *implementation* came from, not the workspace the
        # mission happens to be operating on — a mission can run against any directory.
        import cogos

        repo_root = Path(cogos.__file__).resolve().parent.parent
        prov.git_commit = _git(repo_root, "rev-parse", "HEAD")
        prov.git_branch = _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
        status = _git(repo_root, "status", "--porcelain")
        if prov.git_commit:
            prov.git_dirty = bool(status)
            # porcelain v1 is "XY <path>"; rename entries are "XY <old> -> <new>".
            prov.dirty_files = [
                line[2:].strip().split(" -> ")[-1]
                for line in status.splitlines()[:20]
                if len(line) > 3
            ]
        prov.adapter = str(getattr(adapter, "name", type(adapter).__name__))
        prov.adapter_version = _adapter_version(adapter)
        prov.model_requested = str(getattr(getattr(config, "executive", None), "model", "") or "")
        workspace = Path(getattr(config, "repo_root", "."))
        if workspace.resolve() != repo_root.resolve():
            prov.workspace = str(workspace)
            prov.workspace_commit = _git(workspace, "rev-parse", "HEAD")
        prov.python_version = sys.version.split()[0]
        prov.platform = platform.platform()
        executive = getattr(config, "executive", None)
        if executive is not None and hasattr(executive, "model_dump"):
            payload = json.dumps(executive.model_dump(mode="json"), sort_keys=True, default=str)
            prov.config_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    except Exception:  # noqa: BLE001 - provenance must never fail a mission
        pass
    return prov
