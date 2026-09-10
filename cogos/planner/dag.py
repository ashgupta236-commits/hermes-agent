"""Long-horizon planner: goal hierarchy + task DAG + priority + retry policy.

The plan is a DAG (tasks may have multiple prerequisites), not a tree. The
planner never calls a model; it turns compiled specs into durable tasks,
computes readiness, orders work by expected decision value, detects
structurally repeated failures, and reports progress.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

from cogos.ids import iso_now
from cogos.schemas.cognition import GoalSpec, TaskSpec, parse_json_object
from cogos.schemas.mission import Goal, MissionState, Task, TaskStatus

_TERMINAL = {TaskStatus.DONE, TaskStatus.CANCELLED}


@dataclass
class RetryDecision:
    action: str  # retry|replan|abandon|escalate
    reason: str
    backoff_seconds: float = 0.0


class Planner:
    def __init__(self, state: MissionState):
        self.state = state

    # -- construction --------------------------------------------------------------

    def instantiate_from_specs(self, goals: list[GoalSpec], tasks: list[TaskSpec]) -> dict[str, str]:
        """Create goals/tasks from compiler specs. Returns key->id map."""
        key_to_id: dict[str, str] = {}
        goal_ids: dict[str, str] = {}
        for g in goals:
            goal = Goal(title=g.title, level=g.level or "strategic", rationale=g.rationale)
            self.state.goals.append(goal)
            goal_ids[g.key] = goal.id
        for g in goals:
            if g.parent_key and g.parent_key in goal_ids:
                gid = goal_ids[g.key]
                for goal in self.state.goals:
                    if goal.id == gid:
                        goal.parent_id = goal_ids[g.parent_key]
        for t in tasks:
            task = self._task_from_spec(t, goal_ids)
            self.state.tasks.append(task)
            key_to_id[t.key] = task.id
        for t in tasks:
            task = self.state.task(key_to_id[t.key])
            if task:
                task.depends_on = [key_to_id[d] for d in t.depends_on if d in key_to_id and key_to_id[d] != task.id]
        self._link_tasks_to_unknowns_and_criteria()
        self.compute_ready()
        return key_to_id

    def add_tasks_from_specs(self, tasks: list[TaskSpec]) -> list[Task]:
        goal_ids = {g.title: g.id for g in self.state.goals}
        existing_titles = {t.title.strip().lower(): t.id for t in self.state.tasks}
        created: list[Task] = []
        key_to_id: dict[str, str] = {}
        for t in tasks:
            if t.title.strip().lower() in existing_titles:
                key_to_id[t.key] = existing_titles[t.title.strip().lower()]
                continue
            task = self._task_from_spec(t, {t.goal_key: goal_ids.get(t.goal_key, "")} if t.goal_key in goal_ids else {})
            if t.goal_key and t.goal_key not in goal_ids:
                # goal_key might be a goal id
                if any(g.id == t.goal_key for g in self.state.goals):
                    task.goal_id = t.goal_key
            self.state.tasks.append(task)
            created.append(task)
            key_to_id[t.key] = task.id
        for t in tasks:
            tid = key_to_id.get(t.key)
            task = self.state.task(tid) if tid else None
            if task is None:
                continue
            deps = []
            for d in t.depends_on:
                if d in key_to_id and key_to_id[d] != task.id:
                    deps.append(key_to_id[d])
                elif self.state.task(d) is not None and d != task.id:
                    deps.append(d)
            task.depends_on = sorted(set(task.depends_on + deps))
        self._link_tasks_to_unknowns_and_criteria()
        self.compute_ready()
        return created

    def _task_from_spec(self, t: TaskSpec, goal_ids: dict[str, str]) -> Task:
        return self._with_links(t, goal_ids)

    def _with_links(self, t: TaskSpec, goal_ids: dict[str, str]) -> Task:
        task = Task(
            title=t.title,
            description=t.description,
            goal_id=goal_ids.get(t.goal_key) if t.goal_key else None,
            operation_hint=t.operation_hint,
            parameters=parse_json_object(t.parameters_json),
            priority=max(0.0, min(1.0, t.priority)),
            parallel_safe=t.parallel_safe,
        )
        # Resolve unknown/criterion references by text match now; ids may not exist yet.
        task.parameters.setdefault("_resolves_unknowns", list(t.resolves_unknowns))
        task.parameters.setdefault("_addresses_criteria", list(t.addresses_criteria))
        return task

    def _link_tasks_to_unknowns_and_criteria(self) -> None:
        for task in self.state.tasks:
            want_u = task.parameters.get("_resolves_unknowns") or []
            want_c = task.parameters.get("_addresses_criteria") or []
            for text in want_u:
                for u in self.state.unknowns:
                    if u.id == text or _similar(u.question, text):
                        if u.id not in task.resolves_unknown_ids:
                            task.resolves_unknown_ids.append(u.id)
            for text in want_c:
                for c in self.state.success_criteria:
                    if c.id == text or _similar(c.description, text):
                        if c.id not in task.addresses_criterion_ids:
                            task.addresses_criterion_ids.append(c.id)

    # -- readiness / ordering ------------------------------------------------------------

    def compute_ready(self) -> list[Task]:
        by_id = {t.id: t for t in self.state.tasks}
        ready: list[Task] = []
        for t in self.state.tasks:
            if t.status in (TaskStatus.PENDING, TaskStatus.READY, TaskStatus.BLOCKED):
                if t.status == TaskStatus.BLOCKED and any(b.task_id == t.id and not b.resolved for b in self.state.blocked_operations):
                    continue  # stays isolated until the blocking operation is resolved
                deps = [by_id[d] for d in t.depends_on if d in by_id]
                if any(d.status == TaskStatus.FAILED and d.attempts >= d.max_attempts for d in deps):
                    if t.status != TaskStatus.BLOCKED:
                        t.status = TaskStatus.BLOCKED
                        t.failure_reason = "prerequisite failed permanently"
                    continue
                if any(d.status == TaskStatus.CANCELLED for d in deps):
                    t.status = TaskStatus.CANCELLED
                    continue
                if all(d.status == TaskStatus.DONE for d in deps):
                    if t.status != TaskStatus.READY:
                        t.status = TaskStatus.READY
                        t.updated_at = iso_now()
                    ready.append(t)
                elif t.status == TaskStatus.READY:
                    t.status = TaskStatus.PENDING
        return ready

    def score(self, task: Task) -> float:
        """Expected-value ordering: task priority, unknown value, criterion coverage, verification."""
        s = task.priority
        for uid in task.resolves_unknown_ids:
            for u in self.state.unknowns:
                if u.id == uid and not u.resolved:
                    s += 0.35 * min(u.priority(), 3.0) / 3.0
        for cid in task.addresses_criterion_ids:
            for c in self.state.success_criteria:
                if c.id == cid and not c.satisfied:
                    s += 0.2
        if task.operation_hint in ("verify", "falsify"):
            s += 0.15
        # Unblock the most downstream work first.
        dependents = sum(1 for t in self.state.tasks if task.id in t.depends_on and t.status not in _TERMINAL)
        s += 0.05 * min(dependents, 4)
        s -= 0.1 * task.attempts
        return s

    def next_tasks(self, limit: int = 5) -> list[Task]:
        ready = self.compute_ready()
        ready.sort(key=lambda t: (-self.score(t), t.created_at, t.id))
        return ready[:limit]

    def parallel_batch(self, limit: int = 4) -> list[Task]:
        batch: list[Task] = []
        for t in self.next_tasks(limit=limit * 2):
            if not t.parallel_safe:
                continue
            batch.append(t)
            if len(batch) >= limit:
                break
        return batch

    # -- structure / integrity ----------------------------------------------------------------

    def dag_valid(self) -> tuple[bool, str]:
        by_id = {t.id: t for t in self.state.tasks}
        color: dict[str, int] = {}

        def visit(tid: str, stack: list[str]) -> Optional[str]:
            color[tid] = 1
            for d in by_id.get(tid, Task(title="?")).depends_on:
                if d not in by_id:
                    continue
                c = color.get(d, 0)
                if c == 1:
                    return " -> ".join(stack + [tid, d])
                if c == 0:
                    cyc = visit(d, stack + [tid])
                    if cyc:
                        return cyc
            color[tid] = 2
            return None

        for tid in by_id:
            if color.get(tid, 0) == 0:
                cyc = visit(tid, [])
                if cyc:
                    return False, f"cycle: {cyc}"
        return True, "ok"

    def break_cycles(self) -> int:
        """Remove dependency edges that create cycles (keeps the plan runnable)."""
        removed = 0
        ok, msg = self.dag_valid()
        guard = 0
        while not ok and guard < 100:
            guard += 1
            ids = msg.replace("cycle: ", "").split(" -> ")
            if len(ids) >= 2:
                last, prev = ids[-1], ids[-2]
                t = self.state.task(prev)
                if t and last in t.depends_on:
                    t.depends_on.remove(last)
                    removed += 1
            ok, msg = self.dag_valid()
        return removed

    def prune_irrelevant(self) -> list[Task]:
        """Flag pending tasks that advance no goal, criterion, or unknown (local optimisation guard)."""
        flagged: list[Task] = []
        for t in self.state.tasks:
            if t.status not in (TaskStatus.PENDING, TaskStatus.READY):
                continue
            linked = bool(t.goal_id or t.resolves_unknown_ids or t.addresses_criterion_ids)
            if not linked and t.operation_hint not in ("verify", "synthesize", "complete_mission"):
                t.priority = min(t.priority, 0.2)
                flagged.append(t)
        return flagged

    # -- progress -------------------------------------------------------------------------------

    def progress(self) -> float:
        tasks = [t for t in self.state.tasks if t.status != TaskStatus.CANCELLED]
        task_part = (sum(1 for t in tasks if t.status == TaskStatus.DONE) / len(tasks)) if tasks else 0.0
        crit = self.state.success_criteria
        crit_part = (sum(1 for c in crit if c.satisfied) / len(crit)) if crit else task_part
        return round(0.5 * task_part + 0.5 * crit_part, 3)

    def is_plan_exhausted(self) -> bool:
        return not any(t.status in (TaskStatus.PENDING, TaskStatus.READY, TaskStatus.ACTIVE) for t in self.state.tasks)

    # -- failure policy --------------------------------------------------------------------------

    @staticmethod
    def failure_signature(task: Task, error: str, error_kind: str) -> str:
        norm = " ".join(error.lower().split())[:300]
        return hashlib.sha1(f"{task.operation_hint}|{error_kind}|{norm}".encode("utf-8")).hexdigest()[:16]

    def retry_decision(self, task: Task, error: str, error_kind: str) -> RetryDecision:
        sig = self.failure_signature(task, error, error_kind)
        same_as_last = sig == task.failure_signature
        task.failure_signature = sig
        if error_kind in ("denied", "requires_human"):
            return RetryDecision("escalate", "operation is outside the permitted action space; isolate and continue other work")
        if error_kind == "transient" and task.attempts < task.max_attempts:
            return RetryDecision("retry", "transient failure; retrying with backoff", backoff_seconds=min(2.0 ** task.attempts, 30.0))
        if same_as_last:
            return RetryDecision("replan", "structurally identical failure repeated; a different strategy is required")
        if task.attempts >= task.max_attempts:
            return RetryDecision("abandon", "max attempts reached")
        return RetryDecision("replan", "structural failure; modify approach before retrying")


def _tokens(s: str) -> set[str]:
    return {w for w in "".join(ch.lower() if ch.isalnum() else " " for ch in s).split() if len(w) > 2}


def _similar(a: str, b: str, threshold: float = 0.5) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= threshold or (len(ta & tb) / min(len(ta), len(tb)) >= 0.8)
