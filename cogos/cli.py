"""Command-line interface: ``python -m cogos <command>``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

from cogos.config import load_config
from cogos.schemas.mission import Budget


def _runtime(args: argparse.Namespace):
    from cogos.runtime import Runtime

    overrides: dict[str, Any] = {}
    if getattr(args, "adapter", None):
        overrides.setdefault("executive", {})["adapter"] = args.adapter
    if getattr(args, "model", None):
        overrides.setdefault("executive", {})["model"] = args.model
    if getattr(args, "home", None):
        overrides["home"] = args.home
    cfg = load_config(overrides=overrides)
    return Runtime(cfg, stdout_trace=bool(getattr(args, "verbose", False)))


def _print(obj: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=1, default=str))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (list, dict)):
                print(f"{k}:")
                print("  " + json.dumps(v, indent=1, default=str).replace("\n", "\n  "))
            else:
                print(f"{k}: {v}")
    else:
        print(obj)


def cmd_init(args: argparse.Namespace) -> int:
    rt = _runtime(args)
    cfg_path = Path(rt.config.repo_root) / "cogos.yaml"
    if not cfg_path.exists() and not args.no_config:
        cfg_path.write_text(
            "# cogos runtime configuration (behavioural settings only; secrets stay in the environment)\n"
            "executive:\n  model: claude-fable-5-1\n  adapter: claude_code   # claude_code | anthropic_api | scripted\n  effort: high\n  allow_cheaper_specialist_models: false\n"
            "governance:\n  allow_network: true\n  allow_shell: true\n  always_require_human: [destructive, financial, legally_significant, credential_sensitive]\n"
            "budget:\n  max_cycles: 200\n  max_model_calls: 400\n  max_subagents: 20\n",
            encoding="utf-8",
        )
        print(f"wrote {cfg_path}")
    print(json.dumps(rt.store.health(), indent=1))
    return 0


def cmd_mission_new(args: argparse.Namespace) -> int:
    rt = _runtime(args)
    budget = Budget(max_cycles=args.max_cycles) if args.max_cycles else None
    state = rt.new_mission(args.objective, human_context=args.context or "", budget=budget)
    print(f"mission {state.mission_id} compiled ({state.resources.get('mission_kind')}): {len(state.tasks)} tasks, {len(state.success_criteria)} criteria, {len(state.unknowns)} unknowns")
    if args.run:
        state = rt.run(state.mission_id, max_cycles=args.max_cycles)
        _print(rt.status(state.mission_id), args.json)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    rt = _runtime(args)
    state = rt.resume(args.mission_id, max_cycles=args.max_cycles)
    if state is None:
        print("no unfinished mission to run")
        return 1
    _print(rt.status(state.mission_id), args.json)
    return 0 if state.status.value in ("complete", "active", "paused", "blocked_external") else 2


def cmd_boot(args: argparse.Namespace) -> int:
    from cogos.runtime import boot_summary

    rt = _runtime(args)
    report = rt.boot(brief=args.brief)
    if args.json:
        print(report.model_dump_json(indent=1))
    else:
        print(boot_summary(report))
        if report.workspace and not args.brief:
            print("\n" + report.workspace)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    rt = _runtime(args)
    _print(rt.status(args.mission_id), args.json)
    return 0


def cmd_missions(args: argparse.Namespace) -> int:
    rt = _runtime(args)
    rows = rt.store.list_missions()
    if args.json:
        print(json.dumps(rows, indent=1, default=str))
    else:
        for r in rows:
            print(f"{r['mission_id']}  [{r['status']}]  v{r['version']}  {r['objective'][:90]}")
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    rt = _runtime(args)
    mid = args.mission_id or rt.store.kv_get("last_mission_id")
    if args.json:
        print(json.dumps([t.model_dump(mode="json") for t in rt.store.traces(mid, kind=args.kind, limit=args.limit)], indent=1, default=str))
    else:
        for line in rt.tracer.timeline(mid, limit=args.limit):
            if not args.kind or f" {args.kind} " in line or line.startswith(args.kind):
                print(line)
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    rt = _runtime(args)
    mid = args.mission_id or rt.store.kv_get("last_mission_id")
    _print(rt.explain(mid), args.json)
    return 0


def cmd_workspace(args: argparse.Namespace) -> int:
    rt = _runtime(args)
    mid = args.mission_id or rt.store.kv_get("last_mission_id")
    print(rt.workspace_text(mid))
    return 0


def cmd_answer(args: argparse.Namespace) -> int:
    rt = _runtime(args)
    ev = rt.answer(args.mission_id, args.request_id, args.answer, grant=args.grant)
    print(f"recorded human input {ev.id}; mission reactivated. Run `python -m cogos run {args.mission_id}` to continue.")
    return 0


def cmd_authorize(args: argparse.Namespace) -> int:
    rt = _runtime(args)
    st = rt.authorize(args.mission_id, args.action_class)
    print(f"granted '{args.action_class}' for {st.mission_id}; status={st.status.value}")
    return 0


def cmd_correct(args: argparse.Namespace) -> int:
    rt = _runtime(args)
    ev = rt.correct(args.mission_id, args.text) if args.kind == "correction" else rt.inform(args.mission_id, args.text)
    print(f"recorded {args.kind} {ev.id}")
    return 0


def cmd_event(args: argparse.Namespace) -> int:
    rt = _runtime(args)
    payload = json.loads(args.payload) if args.payload else {}
    ev = rt.emit_event(args.kind, payload, mission_ids=[args.mission_id] if args.mission_id else None, source=args.source)
    print(f"event {ev.id} routed to {ev.routed_to}")
    return 0


def cmd_checkpoint(args: argparse.Namespace) -> int:
    rt = _runtime(args)
    mid = args.mission_id or rt.store.kv_get("last_mission_id")
    path = rt.store.export_snapshot(mid, rt.config.snapshots_dir)
    print(path)
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    rt = _runtime(args)
    st = rt.store.import_snapshot(Path(args.snapshot), overwrite=args.overwrite)
    print(f"imported {st.mission_id} [{st.status.value}]")
    return 0


def cmd_memory(args: argparse.Namespace) -> int:
    rt = _runtime(args)
    if args.action == "stats":
        _print(rt.memory.stats(), args.json)
    elif args.action == "search":
        for m in rt.memory.retrieve(args.query or "", limit=args.limit):
            print(f"[{m.memory_class.value} conf={m.confidence:.2f} imp={m.importance:.2f}] {m.content[:200]}")
    elif args.action == "consolidate":
        _print(rt.memory.consolidate(), args.json)
    elif args.action == "contradictions":
        for a, b in rt.memory.contradictions():
            print(f"- {a.content[:100]}\n  vs {b.content[:100]}")
    return 0


def cmd_skills(args: argparse.Namespace) -> int:
    rt = _runtime(args)
    if args.action == "list":
        for s in rt.skills.list_skills():
            print(f"{s.name} [{s.status}] v{s.version}: {s.description[:100]}")
    elif args.action == "propose":
        res = rt.try_compile_skill(args.mission_id)
        print(json.dumps(res, indent=1) if res else "no candidate skill proposed")
    elif args.action == "evaluate":
        from cogos.evaluation.skill_eval import evaluate_candidate

        print(json.dumps(evaluate_candidate(rt, args.mission_id, args.candidate_id), indent=1, default=str))
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from cogos.evaluation.harness import run_suite

    report = run_suite(args.suite, verbose=args.verbose)
    if args.json:
        print(json.dumps(report, indent=1, default=str))
    else:
        for r in report["results"]:
            print(f"{'PASS' if r['passed'] else 'FAIL'}  {r['name']:<28} {r['summary'][:100]}")
        print(f"\n{report['passed']}/{report['total']} passed; metrics: {json.dumps(report['metrics'], default=str)}")
    if args.write:
        out = Path(args.write)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    return 0 if report["passed"] == report["total"] else 1


def cmd_demo(args: argparse.Namespace) -> int:
    from cogos.evaluation.demo import run_demo

    return run_demo(adapter=args.adapter, objective=args.objective, max_cycles=args.max_cycles, verbose=args.verbose, home=args.home)


def cmd_health(args: argparse.Namespace) -> int:
    rt = _runtime(args)
    _print(rt.store.health(), True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cogos", description="Persistent autonomous cognitive operating system around Claude")
    p.add_argument("--adapter", help="executive adapter: claude_code | anthropic_api | scripted")
    p.add_argument("--model", help="executive model id (default claude-fable-5-1)")
    p.add_argument("--home", help="state directory (default <repo>/.cogos)")
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument("-v", "--verbose", action="store_true", help="stream trace events to stdout")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="initialise the state store and default config")
    s.add_argument("--no-config", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("mission", help="mission commands")
    ms = s.add_subparsers(dest="mission_command", required=True)
    m = ms.add_parser("new", help="compile a new mission from an objective")
    m.add_argument("objective")
    m.add_argument("--context", help="additional human context")
    m.add_argument("--run", action="store_true", help="start running immediately")
    m.add_argument("--max-cycles", type=int)
    m.set_defaults(func=cmd_mission_new)

    s = sub.add_parser("run", help="run/resume a mission (default: highest-priority unfinished)")
    s.add_argument("mission_id", nargs="?")
    s.add_argument("--max-cycles", type=int)
    s.set_defaults(func=cmd_run)
    s = sub.add_parser("resume", help="alias for run without arguments")
    s.add_argument("mission_id", nargs="?")
    s.add_argument("--max-cycles", type=int)
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("boot", help="boot/recovery protocol: reconstruct state and report")
    s.add_argument("--brief", action="store_true")
    s.set_defaults(func=cmd_boot)
    s = sub.add_parser("status", help="mission status")
    s.add_argument("mission_id", nargs="?")
    s.set_defaults(func=cmd_status)
    s = sub.add_parser("missions", help="list missions")
    s.set_defaults(func=cmd_missions)
    s = sub.add_parser("trace", help="show trace timeline")
    s.add_argument("mission_id", nargs="?")
    s.add_argument("--kind")
    s.add_argument("--limit", type=int, default=200)
    s.set_defaults(func=cmd_trace)
    s = sub.add_parser("explain", help="answer the observability questions for a mission")
    s.add_argument("mission_id", nargs="?")
    s.set_defaults(func=cmd_explain)
    s = sub.add_parser("workspace", help="print the global workspace digest")
    s.add_argument("mission_id", nargs="?")
    s.set_defaults(func=cmd_workspace)

    s = sub.add_parser("answer", help="answer a human request")
    s.add_argument("mission_id")
    s.add_argument("request_id")
    s.add_argument("answer")
    s.add_argument("--grant", help="also grant an action class (e.g. destructive)")
    s.set_defaults(func=cmd_answer)
    s = sub.add_parser("authorize", help="grant an action class for a mission")
    s.add_argument("mission_id")
    s.add_argument("action_class")
    s.set_defaults(func=cmd_authorize)
    s = sub.add_parser("correct", help="send a correction or new information to a mission")
    s.add_argument("mission_id")
    s.add_argument("text")
    s.add_argument("--kind", choices=["correction", "information"], default="correction")
    s.set_defaults(func=cmd_correct)
    s = sub.add_parser("event", help="emit an external event")
    s.add_argument("kind")
    s.add_argument("--mission-id")
    s.add_argument("--payload", help="JSON payload")
    s.add_argument("--source", default="external")
    s.set_defaults(func=cmd_event)

    s = sub.add_parser("checkpoint", help="export a mission snapshot")
    s.add_argument("mission_id", nargs="?")
    s.set_defaults(func=cmd_checkpoint)
    s = sub.add_parser("import", help="import a mission snapshot")
    s.add_argument("snapshot")
    s.add_argument("--overwrite", action="store_true")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("memory", help="memory subsystem")
    s.add_argument("action", choices=["stats", "search", "consolidate", "contradictions"])
    s.add_argument("query", nargs="?")
    s.add_argument("--limit", type=int, default=8)
    s.set_defaults(func=cmd_memory)
    s = sub.add_parser("skills", help="skill compiler")
    s.add_argument("action", choices=["list", "propose", "evaluate"])
    s.add_argument("mission_id", nargs="?")
    s.add_argument("candidate_id", nargs="?")
    s.set_defaults(func=cmd_skills)

    s = sub.add_parser("eval", help="run the evaluation suite")
    s.add_argument("--suite", choices=["acceptance", "adversarial", "all"], default="all")
    s.add_argument("--write", help="write JSON report to this path")
    s.set_defaults(func=cmd_eval)
    s = sub.add_parser("demo", help="minimal autonomous demonstration")
    s.add_argument("--objective", default=None)
    s.add_argument("--max-cycles", type=int, default=40)
    s.set_defaults(func=cmd_demo)
    s = sub.add_parser("health", help="store health")
    s.set_defaults(func=cmd_health)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("interrupted; mission state is persisted — run `python -m cogos resume` to continue", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
