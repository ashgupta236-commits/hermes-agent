"""Unified tool fabric.

A :class:`ToolFabric` owns tool specs and handlers, routes each call through
the capability firewall, executes it, scans untrusted output for injection,
bounds output size, and returns a typed :class:`ToolResult`. Tools are plain
callables so the fabric can host filesystem, shell, git, tests, calculators,
web, memory, and MCP-style adapters uniformly.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

from cogos.governance.firewall import CapabilityFirewall
from cogos.governance.immune import scan_for_injection
from cogos.schemas.common import ActionClass, PolicyDecision, TrustLevel
from cogos.schemas.tools import ToolCall, ToolResult, ToolSpec
from cogos.tools.safe_calc import safe_eval

ToolHandler = Callable[[dict[str, Any], "ToolContext"], dict[str, Any]]


class ToolContext:
    def __init__(self, repo_root: Path, workdir: Optional[Path] = None, timeout: int = 120, max_output_chars: int = 20_000, memory: Any = None, mission_id: Optional[str] = None):
        self.repo_root = Path(repo_root)
        self.workdir = Path(workdir) if workdir else self.repo_root
        self.timeout = timeout
        self.max_output_chars = max_output_chars
        self.memory = memory
        self.mission_id = mission_id

    def resolve(self, path: str) -> Path:
        p = Path(path).expanduser()
        return p if p.is_absolute() else (self.workdir / p)


class ToolFabric:
    def __init__(self, firewall: CapabilityFirewall, context: ToolContext):
        self.firewall = firewall
        self.context = context
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, ToolHandler] = {}
        self.call_log: list[ToolResult] = []

    # -- registry ------------------------------------------------------------------

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def specs(self, include_unavailable: bool = False) -> list[ToolSpec]:
        return [s for s in self._specs.values() if include_unavailable or s.available]

    def spec(self, name: str) -> Optional[ToolSpec]:
        return self._specs.get(name)

    def mark_unavailable(self, name: str, reason: str) -> None:
        if name in self._specs:
            self._specs[name].available = False
            self._specs[name].unavailable_reason = reason

    def mark_available(self, name: str) -> None:
        if name in self._specs:
            self._specs[name].available = True
            self._specs[name].unavailable_reason = ""

    def describe(self) -> str:
        lines = []
        for s in self.specs(include_unavailable=True):
            avail = "" if s.available else f"  [UNAVAILABLE: {s.unavailable_reason}]"
            params = ", ".join(f"{k}: {v.get('type', 'any')}" for k, v in (s.parameters_schema.get("properties") or {}).items())
            lines.append(f"- {s.name}({params}) — {s.description}{avail}")
        return "\n".join(lines)

    # -- execution -----------------------------------------------------------------

    def execute(self, call: ToolCall) -> ToolResult:
        spec = self._specs.get(call.tool)
        t0 = time.monotonic()
        if spec is None:
            res = ToolResult(call_id=call.id, tool=call.tool, ok=False, error=f"unknown tool '{call.tool}'", error_kind="unavailable")
            self.call_log.append(res)
            return res
        verdict = self.firewall.check(call, spec)
        if verdict.decision != PolicyDecision.ALLOW:
            kind = "denied" if verdict.decision == PolicyDecision.DENY else "requires_human"
            res = ToolResult(call_id=call.id, tool=call.tool, ok=False, error=verdict.reason, error_kind=kind, verdict=verdict, trust=spec.output_trust)
            self.call_log.append(res)
            return res
        try:
            payload = self._handlers[call.tool](normalise_arguments(call.tool, dict(call.arguments)), self.context)
            ok = bool(payload.pop("ok", True))
            output = str(payload.pop("output", ""))
            error = str(payload.pop("error", ""))
            error_kind = str(payload.pop("error_kind", "" if ok else "structural"))
        except subprocess.TimeoutExpired as exc:
            ok, output, error, error_kind, payload = False, "", f"timeout after {exc.timeout}s", "timeout", {}
        except MissingArgument as exc:
            ok, output, error, error_kind, payload = False, "", str(exc), "structural", {"argument_error": True}
        except FileNotFoundError as exc:
            ok, output, error, error_kind, payload = False, "", str(exc), "structural", {}
        except PermissionError as exc:
            ok, output, error, error_kind, payload = False, "", str(exc), "denied", {}
        except (OSError, ConnectionError) as exc:
            ok, output, error, error_kind, payload = False, "", str(exc), "transient", {}
        except Exception as exc:  # noqa: BLE001 - tool errors are heterogeneous
            ok, output, error, error_kind, payload = False, "", f"{type(exc).__name__}: {exc}", "structural", {}
        truncated = False
        limit = self.context.max_output_chars
        if len(output) > limit:
            output = output[: limit // 2] + f"\n…[{len(output) - limit} chars truncated]…\n" + output[-limit // 2 :]
            truncated = True
        flags = scan_for_injection(output) if spec.output_trust == TrustLevel.UNTRUSTED_EXTERNAL else []
        res = ToolResult(
            call_id=call.id,
            tool=call.tool,
            ok=ok,
            output=output,
            data=payload,
            error=error,
            error_kind=error_kind,
            trust=spec.output_trust,
            duration_ms=int((time.monotonic() - t0) * 1000),
            verdict=verdict,
            injection_flags=flags,
            truncated=truncated,
        )
        self.call_log.append(res)
        return res


# ----------------------------------------------------------------------------------
# Built-in tools
# ----------------------------------------------------------------------------------


def _params(**props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props}


_ALIASES: dict[str, dict[str, str]] = {
    "read_file": {"file": "path", "filename": "path", "file_path": "path", "filepath": "path"},
    "read_document": {"file": "path", "filename": "path", "file_path": "path"},
    "list_dir": {"directory": "path", "dir": "path", "pattern": "glob"},
    "search_text": {"query": "pattern", "regex": "pattern", "directory": "path", "dir": "path"},
    "write_file": {"file": "path", "filename": "path", "file_path": "path", "text": "content", "contents": "content"},
    "delete_file": {"file": "path", "filename": "path"},
    "shell": {"cmd": "command", "script": "command", "directory": "cwd"},
    "run_tests": {"cmd": "command", "test_command": "command"},
    "calculate": {"code": "program", "expr": "expression", "python": "program"},
    "web_fetch": {"uri": "url", "link": "url"},
    "memory_search": {"q": "query", "text": "query"},
    "git": {"argv": "args", "command": "args"},
}


def normalise_arguments(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Accept common argument aliases so a model's reasonable guess still works."""
    out = dict(args)
    for alias, canonical in _ALIASES.get(tool, {}).items():
        if alias in out and canonical not in out:
            out[canonical] = out.pop(alias)
    if tool in ("read_file", "read_document") and "path" not in out and isinstance(out.get("paths"), list) and out["paths"]:
        out["path"] = out["paths"][0]
        out["_extra_paths"] = list(out["paths"][1:])
    if tool == "shell" and "command" not in out and isinstance(out.get("commands"), list):
        out["command"] = " && ".join(str(c) for c in out["commands"])
    if tool == "git" and isinstance(out.get("args"), str):
        out["args"] = out["args"].replace("git ", "", 1).split()
    return out


class MissingArgument(ValueError):
    pass


def _require(args: dict[str, Any], key: str, tool: str) -> Any:
    if key not in args or args[key] in (None, ""):
        raise MissingArgument(f"{tool}: missing required argument '{key}' (got {sorted(args)})")
    return args[key]


def _read_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    path = ctx.resolve(str(_require(args, "path", "read_file")))
    extra = [ctx.resolve(str(p)) for p in args.get("_extra_paths") or []]
    if extra:
        parts = []
        for p in [path, *extra]:
            if p.exists() and p.is_file():
                parts.append(f"=== {p} ===\n" + p.read_text(encoding="utf-8", errors="replace"))
            else:
                parts.append(f"=== {p} === (missing)")
        return {"output": "\n".join(parts), "paths": [str(p) for p in [path, *extra]]}
    if not path.exists():
        return {"ok": False, "error": f"no such file: {path}", "error_kind": "structural"}
    if path.is_dir():
        return {"ok": False, "error": f"is a directory: {path}", "error_kind": "structural"}
    text = path.read_text(encoding="utf-8", errors="replace")
    start = int(args.get("start_line", 1) or 1)
    end = args.get("end_line")
    lines = text.splitlines()
    if end is not None:
        lines = lines[start - 1 : int(end)]
    elif start > 1:
        lines = lines[start - 1 :]
    return {"output": "\n".join(lines), "path": str(path), "line_count": len(text.splitlines())}


def _list_dir(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    path = ctx.resolve(str(args.get("path", ".")))
    pattern = str(args.get("glob", "*"))
    if not path.exists():
        return {"ok": False, "error": f"no such directory: {path}", "error_kind": "structural"}
    entries = sorted(p for p in path.glob(pattern) if ".git" not in p.parts)[: int(args.get("limit", 500))]
    rel = [str(p.relative_to(path)) + ("/" if p.is_dir() else "") for p in entries]
    return {"output": "\n".join(rel), "count": len(rel)}


def _search_text(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    pattern = str(_require(args, "pattern", "search_text"))
    path = ctx.resolve(str(args.get("path", ".")))
    limit = int(args.get("limit", 200))
    rg = shutil.which("rg")
    if rg:
        proc = subprocess.run([rg, "-n", "--no-heading", "-S", "-m", "50", pattern, str(path)], capture_output=True, text=True, encoding="utf-8", timeout=ctx.timeout, check=False)
        lines = proc.stdout.splitlines()[:limit]
        return {"output": "\n".join(lines), "matches": len(lines)}
    rx = re.compile(pattern, re.I)
    hits: list[str] = []
    for p in path.rglob("*"):
        if p.is_file() and ".git" not in p.parts and p.stat().st_size < 2_000_000:
            try:
                for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{p}:{i}:{line.strip()[:200]}")
                        if len(hits) >= limit:
                            break
            except OSError:
                continue
        if len(hits) >= limit:
            break
    return {"output": "\n".join(hits), "matches": len(hits)}


def _write_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    path = ctx.resolve(str(_require(args, "path", "write_file")))
    path.parent.mkdir(parents=True, exist_ok=True)
    content = str(args.get("content", ""))
    mode = "a" if args.get("append") else "w"
    with open(path, mode, encoding="utf-8") as fh:
        fh.write(content)
    return {"output": f"wrote {len(content)} chars to {path}", "path": str(path), "bytes": len(content.encode('utf-8'))}


def _delete_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    path = ctx.resolve(str(_require(args, "path", "delete_file")))
    if not path.exists():
        return {"ok": False, "error": f"no such file: {path}", "error_kind": "structural"}
    if path.is_dir():
        return {"ok": False, "error": "refusing to delete directories", "error_kind": "denied"}
    path.unlink()
    return {"output": f"deleted {path}"}


def _shell(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    command = str(_require(args, "command", "shell"))
    cwd = ctx.resolve(str(args.get("cwd", "."))) if args.get("cwd") else ctx.workdir
    timeout = int(args.get("timeout", ctx.timeout))
    env = {**os.environ, "COGOS_TOOL": "1"}
    proc = subprocess.run(command, shell=True, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env, check=False)  # noqa: S602 - firewall-classified
    out = proc.stdout + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
    ok = proc.returncode == 0
    return {"ok": ok, "output": out, "exit_code": proc.returncode, "error": "" if ok else f"exit code {proc.returncode}", "error_kind": "" if ok else "structural"}


def _git(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    argv = args.get("args") or []
    if isinstance(argv, str):
        argv = argv.split()
    proc = subprocess.run(["git", *map(str, argv)], cwd=str(ctx.workdir), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=ctx.timeout, check=False)
    ok = proc.returncode == 0
    return {"ok": ok, "output": proc.stdout + proc.stderr, "exit_code": proc.returncode, "error": "" if ok else f"git exited {proc.returncode}", "error_kind": "" if ok else "structural"}


_PYTEST_SUMMARY = re.compile(r"(?:=+ )?(?P<summary>\d+ (?:passed|failed|error|errors|skipped|xfailed|xpassed|deselected)[^\n=]*?) in [\d.]+s")
_PYTEST_COUNTS = re.compile(r"(\d+) (passed|failed|error|errors|skipped|xfailed|xpassed)")


def _run_tests(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    command = str(args.get("command") or "python -m pytest -q")
    timeout = int(args.get("timeout", max(ctx.timeout, 600)))
    proc = subprocess.run(command, shell=True, cwd=str(ctx.workdir), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)  # noqa: S602
    out = proc.stdout + proc.stderr
    counts = {k: 0 for k in ("passed", "failed", "error", "skipped")}
    for num, kind in _PYTEST_COUNTS.findall(out):
        key = "error" if kind.startswith("error") else kind
        if key in counts:
            counts[key] += int(num)
    m = _PYTEST_SUMMARY.search(out)
    summary = m.group("summary").strip("= ") if m else (out.strip().splitlines()[-1] if out.strip() else f"exit {proc.returncode}")
    ok = proc.returncode == 0
    return {"ok": ok, "output": out, "exit_code": proc.returncode, "summary": summary, "counts": counts, "command": command, "error": "" if ok else f"tests failed (exit {proc.returncode})", "error_kind": "" if ok else "structural"}


def _calculate(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    program = str(args.get("program") or args.get("expression") or "")
    if not program.strip():
        return {"ok": False, "error": "empty program", "error_kind": "structural"}
    res = safe_eval(program, timeout_seconds=float(args.get("timeout", 5.0)))
    value = res["value"]
    out = res["stdout"]
    if value is not None:
        out = (out + "\n" if out else "") + json.dumps(value, default=str)
    return {"output": out, "value": value, "variables": {k: v for k, v in res["variables"].items() if isinstance(v, (int, float, str, bool, list, dict)) and not str(k).startswith("_")}}


def _web_fetch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    import httpx  # local import: keep import cost off the hot path

    url = str(_require(args, "url", "web_fetch"))
    method = str(args.get("method", "GET")).upper()
    timeout = float(args.get("timeout", 30))
    with httpx.Client(follow_redirects=True, timeout=timeout, headers={"User-Agent": "cogos/0.1 (+research agent)"}) as client:
        resp = client.request(method, url)
    text = resp.text
    ctype = resp.headers.get("content-type", "")
    if "html" in ctype:
        text = _strip_html(text)
    ok = resp.status_code < 400
    return {"ok": ok, "output": text, "status": resp.status_code, "content_type": ctype, "url": str(resp.url), "error": "" if ok else f"HTTP {resp.status_code}", "error_kind": "" if ok else ("transient" if resp.status_code >= 500 else "structural")}


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = re.sub(r"&nbsp;", " ", html)
    html = re.sub(r"&amp;", "&", html)
    html = re.sub(r"&lt;", "<", html)
    html = re.sub(r"&gt;", ">", html)
    return re.sub(r"\s+", " ", html).strip()


def _memory_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if ctx.memory is None:
        return {"ok": False, "error": "memory subsystem not attached", "error_kind": "unavailable"}
    query = str(args.get("query", ""))
    items = ctx.memory.retrieve(query, limit=int(args.get("limit", 8)), mission_id=ctx.mission_id)
    out = "\n".join(f"[{m.memory_class.value}] ({m.confidence:.2f}) {m.content}" for m in items)
    return {"output": out or "(no relevant memories)", "count": len(items), "ids": [m.id for m in items]}


def _read_document(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    path = ctx.resolve(str(_require(args, "path", "read_document")))
    if not path.exists():
        return {"ok": False, "error": f"no such file: {path}", "error_kind": "structural"}
    suffix = path.suffix.lower()
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return {"output": json.dumps(data, indent=1)[:200_000], "format": "json"}
    if suffix in (".csv", ".tsv"):
        import csv

        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh, delimiter="\t" if suffix == ".tsv" else ","))
        preview = "\n".join(",".join(r) for r in rows[:200])
        return {"output": preview, "format": "csv", "rows": len(rows), "columns": rows[0] if rows else []}
    return {"output": path.read_text(encoding="utf-8", errors="replace"), "format": suffix.lstrip(".") or "text"}


def build_default_fabric(firewall: CapabilityFirewall, context: ToolContext) -> ToolFabric:
    fab = ToolFabric(firewall, context)
    fab.register(ToolSpec(name="read_file", description="Read a text file (optionally a line range)", substrate="filesystem", parameters_schema=_params(path={"type": "string"}, start_line={"type": "integer"}, end_line={"type": "integer"}), output_trust=TrustLevel.UNTRUSTED_EXTERNAL), _read_file)
    fab.register(ToolSpec(name="list_dir", description="List directory entries matching a glob", substrate="filesystem", parameters_schema=_params(path={"type": "string"}, glob={"type": "string"})), _list_dir)
    fab.register(ToolSpec(name="search_text", description="Regex search across files (ripgrep when available)", substrate="filesystem", parameters_schema=_params(pattern={"type": "string"}, path={"type": "string"}), output_trust=TrustLevel.UNTRUSTED_EXTERNAL), _search_text)
    fab.register(ToolSpec(name="write_file", description="Write (or append) text to a file inside writable roots", substrate="filesystem", parameters_schema=_params(path={"type": "string"}, content={"type": "string"}, append={"type": "boolean"})), _write_file)
    fab.register(ToolSpec(name="delete_file", description="Delete a single file inside writable roots", substrate="filesystem", parameters_schema=_params(path={"type": "string"})), _delete_file)
    fab.register(ToolSpec(name="shell", description="Run a shell command (classified by the firewall)", substrate="shell", parameters_schema=_params(command={"type": "string"}, cwd={"type": "string"}, timeout={"type": "integer"}), output_trust=TrustLevel.UNTRUSTED_EXTERNAL, deterministic=False), _shell)
    fab.register(ToolSpec(name="git", description="Run a git subcommand in the repository", substrate="git", parameters_schema=_params(args={"type": "array"})), _git)
    fab.register(ToolSpec(name="run_tests", description="Run a test command and parse the result (pytest by default)", substrate="tests", parameters_schema=_params(command={"type": "string"}, timeout={"type": "integer"}), deterministic=False), _run_tests)
    fab.register(ToolSpec(name="calculate", description="Deterministically evaluate arithmetic/statistics in a sandboxed Python subset", substrate="calc", parameters_schema=_params(program={"type": "string"})), _calculate)
    fab.register(ToolSpec(name="web_fetch", description="Fetch a URL (GET) and return text; output is untrusted", substrate="web", parameters_schema=_params(url={"type": "string"}), output_trust=TrustLevel.UNTRUSTED_EXTERNAL, deterministic=False, network=True, default_action_class=ActionClass.REVERSIBLE_EXTERNAL), _web_fetch)
    fab.register(ToolSpec(name="memory_search", description="Relevance-ranked retrieval from long-term memory", substrate="memory", parameters_schema=_params(query={"type": "string"}, limit={"type": "integer"})), _memory_search)
    fab.register(ToolSpec(name="read_document", description="Read a document (text/markdown/json/csv) with light parsing", substrate="documents", parameters_schema=_params(path={"type": "string"}), output_trust=TrustLevel.UNTRUSTED_EXTERNAL), _read_document)
    return fab
