"""The persistent executive loop.

    perceive -> update world model / beliefs -> assess -> select -> perform ->
    observe -> verify -> attribute -> learn -> persist -> replan -> continue

The loop is deterministic Python; cognition happens at typed call sites
through the executive adapter. Every cycle persists mission state, so the
runtime can be stopped and restarted at any point.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from cogos.adapters.base import CognitionRequest, CognitionResponse, ExecutiveModel, ExecutiveUnavailable, UntrustedBlock
from cogos.adapters.continuity import detect as detect_continuity, handoff
from cogos.adapters.schema_utils import schema_for
from cogos.agent_foundry import AgentFoundry
from cogos.beliefs import BeliefGraph
from cogos.beliefs.temporal import TemporalSettler, classify_claim, supersede_overtaken
from cogos.config import CogosConfig
from cogos.events import EventBus
from cogos.executive.controller import Assessment, MetaCognitiveController
from cogos.executive.escalation import ObservationDigest, Tier, classify, deterministic_interpretation, digest_to_interpretation
from cogos.prompts import PROMPTS
from cogos.governance.immune import scan_for_injection, wrap_untrusted
from cogos.ids import iso_now, new_id
from cogos.learning.experience import extract_features
from cogos.learning.retrieval_policy import LearnedRetrieval
from cogos.memory import MemoryManager
from cogos.observability import CalibrationTracker, DecisionJournal, ResourceLedger, Tracer
from cogos.observability.channels import ChannelRecorder
from cogos.persistence import StateStore
from cogos.planner import Planner
from cogos.executive.anchor_service import AnchorService
from cogos.schemas.anchor import HoldStatus
from cogos.schemas.beliefs import Claim, ClaimStatus, Contradiction, Evidence, EvidenceKind
from cogos.verification.reality_anchor import MAX_RESOLUTION_ROUNDS, BlindnessViolation
from cogos.schemas.cognition import (
    EvidenceSpec,
    ObservationInterpretation,
    Replan,
    SpecialistSpec,
    StepDecision,
    Synthesis,
    TaskSpec,
    TaskUpdateSpec,
)
from cogos.schemas.common import ActionClass, EpistemicStatus, OperationKind, PolicyDecision, Provenance, TrustLevel, VerificationStatus
from cogos.schemas.decisions import Decision
from cogos.schemas.events import Event
from cogos.schemas.memory import MemoryClass
from cogos.schemas.mission import (
    Artifact,
    BlockedOperation,
    HumanRequest,
    Lesson,
    MissionState,
    MissionStatus,
    Task,
    TaskStatus,
    Unknown,
)
from cogos.schemas.tools import ToolCall, ToolResult
from cogos.simulation import simulate
from cogos.tools import ToolFabric
from cogos.schemas.verification import VerificationCheck, cite
from cogos.verification import VerificationEngine, VerificationResult
from cogos.verification.engine import mission_completion_check
from cogos.workspace import GlobalWorkspace
from cogos.world_model import WorldModelManager

#: Only contradictions the controller would actually escalate are worth a settlement probe.
CONTRADICTION_SETTLE_FLOOR = 0.5

#: How many times one belief may be attacked before the attack itself is the problem.
MAX_FALSIFICATION_ATTEMPTS = 2

EXECUTIVE_KINDS = {"compile", "select", "interpret", "synthesize", "replan", "challenge", "verify"}


@dataclass
class OperationOutcome:
    operation: OperationKind
    task_id: str = ""
    tool_results: list[ToolResult] = field(default_factory=list)
    specialist_reports: list[dict[str, Any]] = field(default_factory=list)
    reasoning_output: str = ""
    calculation_result: Any = None
    calculation_error: str = ""
    simulation_result: Optional[dict[str, Any]] = None
    verification: Optional[VerificationResult] = None
    synthesis: Optional[Synthesis] = None
    human_request: Optional[HumanRequest] = None
    waited_for: str = ""
    completed: bool = False
    completion_refusal: str = ""
    errors: list[str] = field(default_factory=list)
    untrusted: list[UntrustedBlock] = field(default_factory=list)
    disagreements: list[dict[str, Any]] = field(default_factory=list)
    anchor: Optional[Any] = None


@dataclass
class CycleResult:
    cycle: int
    decision: Optional[StepDecision]
    outcome: Optional[OperationOutcome]
    assessment: Assessment
    stop: bool = False
    stop_reason: str = ""


class Executive:
    def __init__(
        self,
        config: CogosConfig,
        store: StateStore,
        adapter: ExecutiveModel,
        fabric: ToolFabric,
        memory: MemoryManager,
        tracer: Tracer,
        events: EventBus,
        foundry: Optional[AgentFoundry] = None,
        controller: Optional[MetaCognitiveController] = None,
        sleep_fn: Any = time.sleep,
    ):
        self.config = config
        self.store = store
        self.adapter = adapter
        self.fabric = fabric
        self.memory = memory
        self.tracer = tracer
        self.events = events
        self.foundry = foundry or AgentFoundry(adapter, config)
        self.controller = controller or MetaCognitiveController()
        self.calibration = CalibrationTracker(store)
        self.workspace = GlobalWorkspace(config.workspace_max_chars)
        self.anchors = AnchorService(self)
        self.channels = ChannelRecorder(store, run_id=new_id("run"))
        self.continuity = detect_continuity(adapter)
        self.retrieval = LearnedRetrieval()
        self._cognition_log: list[tuple[str, CognitionResponse]] = []
        self._sleep = sleep_fn
        self._tool_history: list[bool] = []

    # ==========================================================================
    # Run / cycle
    # ==========================================================================

    def run(self, mission_id: str, max_cycles: Optional[int] = None) -> MissionState:
        state = self.store.load_mission(mission_id)
        if state is None:
            raise KeyError(f"unknown mission {mission_id}")
        self.tracer.set_mission(mission_id)
        if state.status == MissionStatus.DRAFT:
            state.status = MissionStatus.ACTIVE
            state.timestamps.started_at = state.timestamps.started_at or iso_now()
        if state.status in (MissionStatus.PAUSED, MissionStatus.BLOCKED_EXTERNAL):
            if self.events.pending_for(mission_id) or state.status == MissionStatus.PAUSED:
                state.status = MissionStatus.ACTIVE
        self._apply_grants(state)
        self.store.save_mission(state, "run_started")
        limit = max_cycles if max_cycles is not None else state.budget.max_cycles
        ran = 0
        while state.status == MissionStatus.ACTIVE and ran < limit:
            t0 = time.monotonic()
            result = self.cycle(state)
            state.usage.wall_clock_seconds += time.monotonic() - t0
            self._persist(state, result)
            ran += 1
            if result.stop:
                break
        self._checkpoint(state, final=True)
        return state

    def cycle(self, state: MissionState) -> CycleResult:
        state.usage.cycles += 1
        cycle_no = state.usage.cycles
        self.tracer.set_cycle(cycle_no)
        self.tracer.emit("cycle_start", f"cycle {cycle_no}", cycle=cycle_no, data={"status": state.status.value, "progress": state.progress})
        ledger = ResourceLedger(state.usage)
        beliefs = BeliefGraph(state)
        planner = Planner(state)
        world = WorldModelManager(state.world_model)

        # 1. perceive ---------------------------------------------------------------
        observations = self._perceive(state)
        # 2. update ------------------------------------------------------------------
        self._update(state, observations, beliefs, world, planner)
        # 3. assess ------------------------------------------------------------------
        over = ledger.over_budget(state.budget)
        verification_pending = self._verification_pending(state)
        # Before any contradiction can drive expensive falsification, try to settle it by
        # *looking*. If the dispute is about directly observable current state, the filesystem
        # is authoritative and costs a millisecond; the live run spent 48% of its budget
        # deliberating over a question a `list_dir` answers.
        self._settle_temporal_contradictions(state)
        # Closure (area E): read the completion gate now, so a mission that has done its work and
        # is only missing evidence prefers the already-planned step that would bind it. The gate is
        # unchanged; only task ordering and what the executive is told about it change.
        closure = self._assess_closure(state)
        planner._closure = closure
        falsify_target = self._falsification_target(state, beliefs)
        assessment = self.controller.assess(
            state,
            tool_results_history=self._tool_history,
            verification_pending=verification_pending,
            falsification_target=falsify_target,
            challenged=bool(state.resources.get("controller", {}).get("challenged")),
            over_budget=over,
        )
        self.tracer.emit("assess", "; ".join(assessment.directives) or "no directives", cycle=cycle_no, data=assessment.model_dump(mode="json"))
        if over:
            state.status = MissionStatus.PAUSED
            state.notes.append(f"paused: {over}")
            self.tracer.emit("blocked", f"budget: {over}", cycle=cycle_no)
            return CycleResult(cycle_no, None, None, assessment, stop=True, stop_reason=over)
        for d in assessment.directives:
            if d.startswith("terminate_branch:"):
                tid = d.split(":", 1)[1].split()[0]
                t = state.task(tid)
                if t and t.status == TaskStatus.FAILED and t.attempts >= t.max_attempts:
                    self._cancel_dependents(state, t)

        # 4. select ------------------------------------------------------------------
        try:
            decision = self._select(state, planner, beliefs, world, assessment, verification_pending, falsify_target)
        except ExecutiveUnavailable as exc:
            return self._block_on_executive(state, assessment, str(exc), cycle_no)
        if decision is None:
            state.status = MissionStatus.PAUSED
            state.notes.append("paused: executive could not select a step")
            return CycleResult(cycle_no, None, None, assessment, stop=True, stop_reason="no decision")
        self.tracer.emit("select", f"{decision.operation.value}: {decision.rationale[:200]}", cycle=cycle_no, data={"operation": decision.operation.value, "task_id": decision.task_id, "rationale": decision.rationale, "confidence": decision.confidence, "tool": decision.tool_calls[0].tool if decision.tool_calls else "", "alternatives": decision.alternatives_considered, "consequential": decision.consequential})
        if decision.consequential:
            self._journal_decision(state, decision, assessment)

        # 5. perform ------------------------------------------------------------------
        # R2: from here to the end of the cycle, the four channels are recorded separately.
        record = self.channels.begin(state, decision, cycle_no, branch=decision.task_id or "mission")
        marks = (len(state.observations), len(state.tests), len(state.verifications))
        fabric_mark = len(self.fabric.call_log)
        record.attempted.effective_permissions = sorted(self.fabric.available_tool_names())
        task = state.task(decision.task_id) if decision.task_id else None
        if task is not None:
            task.status = TaskStatus.ACTIVE
            task.attempts += 1
            task.updated_at = iso_now()
        try:
            outcome = self._perform(state, decision, task, beliefs, planner, ledger, assessment)
        except ExecutiveUnavailable as exc:
            if task is not None:
                task.status = TaskStatus.PENDING
            return self._block_on_executive(state, assessment, str(exc), cycle_no)

        for res in self.fabric.call_log[fabric_mark:]:
            self.channels.record_tool_call(record, res)
        for kind, resp in self._cognition_log:
            self.channels.record_model_call(record, kind, resp)
        self._cognition_log.clear()
        self.channels.record_environment(record, state, *marks)
        if outcome.anchor is not None:
            self.channels.record_anchor(record, outcome.anchor)
        self.channels.finish(record, state)
        for d in record.discrepancies:
            if d.severity >= 0.8:
                self.tracer.emit("discrepancy", f"{d.kind.value}: {d.detail[:200]}", cycle=cycle_no, data=d.model_dump(mode="json"))

        # 6/7. observe + interpret ---------------------------------------------------
        interp = self._interpret(state, decision, outcome, task, beliefs, assessment)
        self._apply_interpretation(state, interp, outcome, task, beliefs, world, planner)
        # 8. attribute success/failure -------------------------------------------------
        stop, reason = self._attribute(state, decision, outcome, task, planner, interp)
        # 9. learn ---------------------------------------------------------------------
        self._learn(state, decision, outcome, interp, task)
        # 10. replan -------------------------------------------------------------------
        planner.compute_ready()
        planner.prune_irrelevant()
        state.progress = planner.progress()
        state.confidence = self._mission_confidence(state)
        for u in state.unknowns:
            if not u.resolved and task is not None and u.id in task.resolves_unknown_ids:
                u.attempts += 1
        if outcome.completed:
            stop, reason = True, "mission complete"
        return CycleResult(cycle_no, decision, outcome, assessment, stop=stop, stop_reason=reason)

    # ==========================================================================
    # Phases
    # ==========================================================================

    def _perceive(self, state: MissionState) -> list[Event]:
        pending = self.events.pending_for(state.mission_id)
        for ev in pending:
            self.tracer.emit("event", f"{ev.kind} from {ev.source}", data={"event_id": ev.id, "trusted": ev.trusted, "payload_keys": list(ev.payload.keys())[:10]})
        return pending

    def _update(self, state: MissionState, observations: list[Event], beliefs: BeliefGraph, world: WorldModelManager, planner: Planner) -> None:
        world.advance_time()
        for ev in observations:
            affects = self.events.affected_state(ev, state.mission_id)
            if ev.kind == "human_input" and ev.trusted:
                rid = str(ev.payload.get("request_id", ""))
                for hr in state.human_requests:
                    if hr.id == rid or (not rid and not hr.answered):
                        hr.answered = True
                        hr.answer = str(ev.payload.get("answer", ""))
                        break
                if ev.payload.get("grant"):
                    state.permissions.setdefault("grants", []).append(str(ev.payload["grant"]))
                    self._apply_grants(state)
                if ev.payload.get("correction"):
                    state.notes.append(f"human correction: {ev.payload['correction']}")
                    state.known_facts.append(_fact(str(ev.payload["correction"]), "human"))
                if ev.payload.get("new_information"):
                    state.known_facts.append(_fact(str(ev.payload["new_information"]), "human"))
            elif ev.kind == "new_evidence":
                ev_spec = ev.payload.get("evidence") or {}
                if ev_spec.get("summary"):
                    prov = Provenance(source=str(ev_spec.get("source", ev.source)), trust=TrustLevel.UNTRUSTED_EXTERNAL, method="event", lineage=list(ev_spec.get("lineage") or []))
                    beliefs.add_evidence(Evidence(summary=str(ev_spec["summary"]), supports_claim_ids=list(ev_spec.get("supports") or []), contradicts_claim_ids=list(ev_spec.get("contradicts") or []), kind=EvidenceKind(ev_spec.get("kind", "secondary")), provenance=prov, scope=str(ev_spec.get("scope", "")), freshness=ev_spec.get("freshness")))
            elif ev.kind in ("test_completed", "job_completed"):
                state.notes.append(f"event {ev.kind}: {json.dumps(ev.payload, default=str)[:300]}")
            for cid in affects.get("claims", []):
                c = state.claim(cid)
                if c:
                    from cogos.schemas.beliefs import ClaimStatus

                    c.last_verified_at = None
                    if c.status in (ClaimStatus.SUPPORTED, ClaimStatus.ESTABLISHED):
                        # New information invalidates the standing verification: demote so the
                        # claim cannot satisfy a criterion until it is re-established.
                        c.status = ClaimStatus.STALE
                        c.confidence = _clamp(0.5 + (c.confidence - 0.5) * 0.7)
                    c.updated_at = iso_now()
                    state.notes.append(f"claim {cid} demoted to stale for re-verification by event {ev.id}")
            for uid in affects.get("unknowns", []):
                for u in state.unknowns:
                    if u.id == uid:
                        u.resolved = False
            for tid in affects.get("tasks", []):
                t = state.task(tid)
                if t and t.status in (TaskStatus.BLOCKED, TaskStatus.FAILED):
                    t.status = TaskStatus.PENDING
            self.events.mark_handled(ev.id, state.mission_id)
        # A blocked operation is cleared only by an actual authorization grant (see
        # _apply_grants). An answered request is not consent: "deny" is an answer too, and
        # substring-matching a question against an operation reactivated denied work forever.
        self._apply_grants(state)
        self._record_denied_authorization(state)
        beliefs.recompute()
        beliefs.detect_contradictions()
        beliefs.mark_stale(max_age_days=90)
        planner.compute_ready()
        if state.status == MissionStatus.ACTIVE and state.unanswered_human_requests():
            # Re-evaluate whether independent work remains for each request.
            has_ready = bool(planner.compute_ready())
            for hr in state.unanswered_human_requests():
                hr.independent_work_remaining = has_ready

    # ------------------------------------------------------------------------------

    def _verification_pending(self, state: MissionState) -> list[str]:
        out = []
        for t in state.tasks:
            if t.status == TaskStatus.DONE and not t.verification_attempt_ids:
                p = t.parameters
                if p.get("commands") or p.get("test_command") or p.get("verify_commands") or t.artifact_ids or p.get("research"):
                    out.append(t.id)
        return out

    def _assess_closure(self, state: MissionState) -> Any:
        """What, if anything, stands between this mission and an honest completion."""
        from cogos.verification.closure import assess

        try:
            gate = mission_completion_check(state)
        except Exception as exc:  # noqa: BLE001 - closure is advisory; never break the cycle
            self.tracer.emit("error", f"closure assessment failed: {exc}")
            return None
        closure = assess(state, gate)
        if closure.blocked and closure.candidate_task_ids:
            self.tracer.emit(
                "closure",
                f"{closure.summary()[:180]} — {len(closure.candidate_task_ids)} planned task(s) would bind it",
                data={
                    "needs": [n.model_dump(mode="json") for n in closure.needs],
                    "candidate_task_ids": closure.candidate_task_ids,
                    "substantive_work_done": closure.substantive_work_done,
                },
            )
        return closure

    def _settle_temporal_contradictions(self, state: MissionState) -> list[str]:
        """Deterministically close contradictions that current state already answers.

        Runs before the controller sees the contradiction set, so a dispute the world has already
        settled never becomes a `must_falsify` directive. Genuine disputes — where current state
        does not overtake either claim — are left untouched and escalate exactly as before.
        """
        settler = TemporalSettler(self.fabric, self.tracer)
        roots = [str(self.config.repo_root)]
        extra = state.resources.get("root")
        if extra and str(extra) not in roots:
            roots.append(str(extra))
        settled: list[str] = []
        for contradiction in list(state.unresolved_contradictions()):
            if contradiction.severity < CONTRADICTION_SETTLE_FLOOR:
                continue
            try:
                result = settler.settle(contradiction, state, roots=roots)
            except Exception as exc:  # noqa: BLE001 - settlement is an optimisation, never load-bearing
                self.tracer.emit("error", f"temporal settlement failed for {contradiction.id}: {exc}")
                continue
            if result.settled:
                settled.append(contradiction.id)
        return settled

    def _falsification_target(self, state: MissionState, beliefs: BeliefGraph) -> Optional[dict[str, Any]]:
        targets = beliefs.falsification_targets(limit=1)
        if not targets:
            return None
        tgt = dict(targets[0])
        key = tgt.get("claim_id") or tgt.get("hypothesis_id") or ""
        controller = state.resources.setdefault("controller", {})
        falsified = list(controller.get("falsified", []))
        attempts = falsified.count(key) if key else 0
        attempted = key in set(falsified)
        tgt["attempted"] = attempted
        tgt["attempts"] = attempts
        tgt["conditions"] = tgt.get("falsification_conditions") or tgt.get("disconfirming_observations") or []
        # Falsification is bounded per target. Without this, an unresolved contradiction re-issues
        # `must_falsify` on the same belief every single cycle — which is exactly what happened
        # live: 21 offline cycles and four live ones spent re-attacking one belief, while the
        # deliverables the mission existed to produce were never written. A belief that has
        # survived repeated falsification is not settled by attacking it again; it is recorded as
        # resistant and the mission moves on.
        if attempts >= MAX_FALSIFICATION_ATTEMPTS:
            exhausted: list[str] = controller.setdefault("falsification_exhausted", [])
            if key and key not in exhausted:
                exhausted.append(key)
                state.notes.append(
                    f"falsification exhausted for {key} after {attempts} attempts; recorded as resistant to falsification "
                    "rather than re-attacked"
                )
                self.tracer.emit(
                    "verify",
                    f"falsification bound reached for {key} after {attempts} attempts",
                    data={"target": key, "attempts": attempts},
                )
            return None
        return None if attempted and not state.unresolved_contradictions() else tgt

    # ------------------------------------------------------------------------------

    def _select(self, state: MissionState, planner: Planner, beliefs: BeliefGraph, world: WorldModelManager, assessment: Assessment, verification_pending: list[str], falsify_target: Optional[dict[str, Any]]) -> Optional[StepDecision]:
        ready = planner.next_tasks(limit=6)
        memory_lines: list[str] = []
        # L2: the learned policy picks among three validated, equivalent-effect retrieval
        # strategies, and is then scored on what the choice actually recalled.
        features = extract_features(state, str(state.resources.get("mission_kind", "general")))
        strategy, why = self.retrieval.choose(features)
        params = self.retrieval.parameters(strategy)
        quarantined = 0
        retrieved = 0
        try:
            mems = self.memory.retrieve(state.objective, limit=int(params["limit"]), mission_id=state.mission_id, classes=params["classes"])
            retrieved = len(mems)
            for m in mems:
                flags = scan_for_injection(m.content)
                if flags:
                    # Poisoned memory is quarantined from the workspace and reported, never followed.
                    quarantined += 1
                    self.tracer.emit("blocked", f"memory {m.id} quarantined: injection flags {flags}", data={"memory_id": m.id, "flags": flags})
                    state.notes.append(f"memory {m.id} quarantined (injection flags {flags})")
                    continue
                memory_lines.append(f"[{m.memory_class.value} {m.confidence:.2f}] {m.content[:200]}")
        except Exception as exc:  # noqa: BLE001 - memory failures must not stop cognition
            self.tracer.emit("error", f"memory retrieval failed: {exc}")
        value = self.retrieval.record(used=len(memory_lines), retrieved=retrieved, quarantined=quarantined)
        if retrieved:
            self.tracer.emit(
                "policy",
                f"retrieval strategy '{strategy}' ({why}) recalled {len(memory_lines)}/{retrieved}, {quarantined} quarantined",
                data={"policy": self.retrieval.bandit.name, "strategy": strategy, "context": features.bucket(), "estimate": value, "updates": self.retrieval.bandit.update_count},
            )
        recent = [t.summary for t in self.store.traces(state.mission_id, limit=400) if t.kind in ("operation", "verify", "failure", "specialist")][-8:]
        view = self.workspace.build(
            state,
            ready_tasks=ready,
            belief_lines=beliefs.summary_for_workspace(limit=8),
            world_lines=world.snapshot_summary(limit=8),
            memory_lines=memory_lines,
            recent_observations=recent,
            assessment={k: v for k, v in assessment.model_dump().items() if k in ("confidence", "uncertainty", "stakes", "contradiction_level", "progress_rate", "stalled_cycles", "p_strategy_wrong", "ev_additional_work", "budget_pressure")},
            directives=assessment.directives,
            tools_text=self.fabric.describe(),
            skills_text=str(state.resources.get("skills_text", "")),
        )
        tournament = None
        if len([h for h in state.hypotheses if h.status in ("active", "leading")]) >= 2:
            tournament = beliefs.tournament().model_dump(mode="json")
        allowed_ops = [o.value for o in OperationKind]
        criteria = [{"id": c.id, "description": c.description, "satisfied": c.satisfied, "verification_method": c.verification_method} for c in state.success_criteria]
        blocked = [b for b in state.blocked_operations if not b.resolved]
        metadata = {
            "ready_tasks": [self._task_view(t) for t in ready],
            "criteria": criteria,
            "unknowns": [{"id": u.id, "question": u.question, "priority": u.priority(), "attempts": u.attempts} for u in sorted(state.open_unknowns(), key=lambda u: -u.priority())[:8]],
            "contradictions": [c.model_dump(mode="json") for c in state.unresolved_contradictions()[:5]],
            "directives": assessment.directives,
            "closure": (
                {
                    "completion_blocked_on": [{"check": n.check, "missing": n.detail[:200], "criterion_ids": n.criterion_ids} for n in planner._closure.needs],
                    "tasks_that_would_bind_the_missing_evidence": planner._closure.candidate_task_ids,
                    "substantive_work_done": planner._closure.substantive_work_done,
                    "guidance": "These predicates are what the completion gate is refusing on. A planned task that binds "
                    "evidence to them is worth more than further deliberation. Verification still has to actually run — "
                    "nothing is satisfied by asserting it.",
                }
                if getattr(planner, "_closure", None) is not None and planner._closure.blocked
                else None
            ),
            "verification_pending": verification_pending,
            "criteria_verification_pending": self._criteria_pass_useful(state, planner, verification_pending),
            "synthesis_exists": bool(state.synthesis),
            "human_requests": [h.model_dump(mode="json") for h in state.unanswered_human_requests()],
            "independent_work_remaining": bool(ready),
            "plan_exhausted": planner.is_plan_exhausted(),
            "allowed_operations": allowed_ops,
            "tools": [s.name for s in self.fabric.specs()],
            "research_tools": [n for n in ("web_search", "web_fetch", "read_file", "search_text") if (spec_n := self.fabric.spec(n)) is not None and spec_n.available] + ["web_search"],
            "falsification_target": falsify_target,
            "tournament": tournament,
            "belief_lines": beliefs.summary_for_workspace(limit=8),
            "blocked_external": state.status == MissionStatus.BLOCKED_EXTERNAL or bool(blocked and not ready),
            "blocked_event_kind": "human_input",
            "progress": state.progress,
        }
        prompt = view.text + "\n\nOPERATIONS: " + ", ".join(allowed_ops) + "\nChoose the next step and return the StepDecision JSON."
        if tournament:
            prompt += "\n\nHYPOTHESIS TOURNAMENT:\n" + json.dumps(tournament, default=str)[:3000]
        req = CognitionRequest(kind="select", system_prompt=PROMPTS["select"], prompt=prompt, schema_name="StepDecision", output_schema=schema_for(StepDecision), model=self.config.executive.model, effort=assessment.effort, timeout_seconds=self.config.executive.call_timeout_seconds, mission_id=state.mission_id, metadata=metadata)
        resp = self._cognition(state, req)
        if not resp.ok:
            self.tracer.emit("error", f"select failed: {resp.error[:200]}", data={"error_kind": resp.error_kind})
            return self._fallback_select(state, planner, ready, verification_pending)
        try:
            decision = StepDecision.model_validate(resp.parsed)
        except Exception as exc:  # noqa: BLE001
            self.tracer.emit("error", f"invalid StepDecision: {exc}")
            return self._fallback_select(state, planner, ready, verification_pending)
        # Hard governance: runtime, not model, decides completion; verification precedes claiming criteria.
        if decision.operation == OperationKind.COMPLETE_MISSION and verification_pending:
            decision = StepDecision(operation=OperationKind.VERIFY, task_id=verification_pending[0], rationale="runtime override: unverified outputs must be verified before completion", confidence=decision.confidence)
        if decision.operation == OperationKind.VERIFY and not decision.task_id and verification_pending:
            # A verify step with no target repeatedly re-ran the criteria pass without ever
            # clearing the pending output; bind it to the actual unverified task.
            decision = decision.model_copy(update={"task_id": verification_pending[0], "rationale": f"runtime override: bound verification to unverified task ({decision.rationale[:120]})"})
        if decision.task_id and state.task(decision.task_id) is None:
            decision.task_id = ""
        return decision

    def _criteria_pass_useful(self, state: MissionState, planner: Planner, verification_pending: list[str]) -> bool:
        """A criteria pass is only worth running when it can still change something."""
        open_criteria = [c for c in state.success_criteria if not c.satisfied]
        if not open_criteria or verification_pending or not planner.is_plan_exhausted():
            return False
        controller = state.resources.get("controller", {})
        if controller.get("criteria_checked_sig") == self._criteria_sig(state):
            return False  # nothing has changed since the last pass
        undecidable = set(controller.get("undecidable_criteria", []))
        return not all(c.id in undecidable for c in open_criteria)

    @staticmethod
    def _criteria_sig(state: MissionState) -> list[int]:
        return [len(state.completed_tasks()), len(state.tests), len(state.evidence), len(state.claims), len(state.unresolved_contradictions()), int(bool(state.synthesis))]

    def _fallback_select(self, state: MissionState, planner: Planner, ready: list[Task], verification_pending: list[str]) -> Optional[StepDecision]:
        if verification_pending:
            return StepDecision(operation=OperationKind.VERIFY, task_id=verification_pending[0], rationale="fallback: verify pending outputs", confidence=0.5)
        if ready:
            from cogos.adapters.scripted import HeuristicExecutive

            h = HeuristicExecutive()
            return StepDecision.model_validate(h._decision_for_task(self._task_view(ready[0]), {}).model_dump(mode="json"))
        if not state.synthesis:
            return StepDecision(operation=OperationKind.SYNTHESIZE, rationale="fallback: synthesise", confidence=0.4)
        return StepDecision(operation=OperationKind.COMPLETE_MISSION, rationale="fallback: attempt completion gate", confidence=0.3)

    @staticmethod
    def _task_view(t: Task) -> dict[str, Any]:
        return {"id": t.id, "title": t.title, "description": t.description, "operation_hint": t.operation_hint, "parameters": {k: v for k, v in t.parameters.items() if not k.startswith("_")}, "priority": t.priority, "attempts": t.attempts, "depends_on": t.depends_on, "resolves_unknown_ids": t.resolves_unknown_ids, "failure_reason": t.failure_reason}

    # ------------------------------------------------------------------------------

    def _perform(self, state: MissionState, decision: StepDecision, task: Optional[Task], beliefs: BeliefGraph, planner: Planner, ledger: ResourceLedger, assessment: Assessment) -> OperationOutcome:
        op = decision.operation
        out = OperationOutcome(operation=op, task_id=task.id if task else "")
        tool_ops = {OperationKind.INSPECT_FILES, OperationKind.EXECUTE_CODE, OperationKind.RUN_EXPERIMENT, OperationKind.USE_EXTERNAL_TOOL, OperationKind.EXECUTE_ACTION, OperationKind.SEARCH, OperationKind.RETRIEVE_MEMORY}
        if op in tool_ops:
            calls = decision.tool_calls or ([] if not task else self._calls_from_task(task))
            if not calls:
                out.errors.append("no tool calls specified")
            # Web search has no local substrate: delegate it to a researcher specialist (Claude Code WebSearch).
            searches = [c for c in calls if c.tool == "web_search" and self.fabric.spec("web_search") is None]
            if searches:
                calls = [c for c in calls if c.tool != "web_search"]
                queries = [str(c.arguments().get("query") or c.purpose) for c in searches]
                spec = SpecialistSpec(role="researcher", objective="Search the web and report sourced findings for: " + "; ".join(queries), tools=["web_search", "web_fetch"], context_keys=["unknowns"], max_turns=12, evidence_standard="primary sources; cite URL, date, scope; note repeated sources")
                sd = AgentFoundry.should_spawn(spec, expertise_value=True, budget_left=state.budget.max_subagents - state.usage.subagents_spawned)
                run = self.foundry.run(spec, state) if sd.spawn else None
                if run is None:
                    out.errors.append(f"web search not delegated: {sd.reason}")
                    self.tracer.emit("specialist", f"declined web-search researcher: {sd.reason}")
                ledger.add_subagent() if run is not None else None
                if run is not None:
                    ledger.add_model_call(run.response)
                if run is not None and run.report is not None:
                    rep = run.report.model_dump(mode="json")
                    rep["_injection_flags"] = run.injection_flags
                    out.specialist_reports.append(rep)
                    self.tracer.emit("specialist", f"researcher (web search): {run.report.conclusion[:160]}", data={"role": "researcher", "queries": queries, "model": run.model})
                elif run is not None:
                    out.errors.append(f"web search specialist failed: {run.response.error[:160]}")
            for spec in calls:
                res = self._tool(state, ToolCall(tool=spec.tool, arguments=spec.arguments(), task_id=out.task_id, purpose=spec.purpose), ledger, task)
                out.tool_results.append(res)
                if res.trust == TrustLevel.UNTRUSTED_EXTERNAL and res.ok:
                    out.untrusted.append(wrap_untrusted(f"{res.tool}:{spec.purpose[:40]}", str(spec.arguments().get("path") or spec.arguments().get("url") or spec.arguments().get("command") or res.tool), res.output))
        elif op == OperationKind.CALCULATE:
            res = self._tool(state, ToolCall(tool="calculate", arguments={"program": decision.calculation or (task.parameters.get("program") if task else "") or "0"}, task_id=out.task_id, purpose="calculate"), ledger, task)
            out.tool_results.append(res)
            out.calculation_result = res.data.get("value") if res.ok else None
            out.calculation_error = res.error
        elif op == OperationKind.SIMULATE:
            scenario = {}
            try:
                scenario = json.loads(decision.simulation_json) if decision.simulation_json else {}
            except json.JSONDecodeError:
                scenario = {}
            if not scenario and task:
                scenario = task.parameters.get("scenario") or {}
            if not scenario or not scenario.get("options"):
                scenario = self._scenario_from_state(state)
            try:
                sim = simulate(scenario)
                out.simulation_result = sim.model_dump(mode="json")
                self.tracer.emit("operation", f"simulated '{sim.question[:80]}': best={sim.best_option} margin={sim.margin:.3f} robust={sim.robust_best}", data={"warnings": sim.warnings, "results": [r.model_dump(mode="json") for r in sim.results]})
            except Exception as exc:  # noqa: BLE001
                out.errors.append(f"simulation failed: {exc}")
                self.tracer.emit("failure", f"simulation failed: {exc}")
        elif op == OperationKind.DIRECT_REASONING:
            out.reasoning_output = decision.reasoning_output or decision.rationale
            self.tracer.emit("operation", f"reasoning: {out.reasoning_output[:160]}")
        elif op in (OperationKind.INSTANTIATE_SPECIALIST, OperationKind.PARALLEL_WORKSTREAMS, OperationKind.FALSIFY):
            specs = decision.specialists or (self._specs_from_task(task) if task else [])
            if op == OperationKind.FALSIFY:
                key = ""
                tgt = self._falsification_target(state, beliefs)
                if tgt:
                    key = tgt.get("claim_id") or tgt.get("hypothesis_id") or ""
                    if not specs:
                        specs = [SpecialistSpec(role="skeptic", objective=f"Attempt to falsify: {tgt.get('statement', '')}. Disconfirming conditions: {tgt.get('conditions', [])}", independent=True, context_keys=["evidence", "unknowns"], max_turns=10)]
                if key:
                    state.resources.setdefault("controller", {}).setdefault("falsified", []).append(key)
            if not specs:
                out.errors.append("no specialist specification")
            budget_left = state.budget.max_subagents - state.usage.subagents_spawned
            runs = []
            to_run = []
            for spec in specs:
                sd = AgentFoundry.should_spawn(spec, parallel_value=len(specs) > 1, isolation_value=spec.independent, expertise_value=True, budget_left=budget_left - len(to_run))
                if not sd.spawn:
                    out.errors.append(f"specialist '{spec.role}' not spawned: {sd.reason}")
                    self.tracer.emit("specialist", f"declined {spec.role}: {sd.reason}")
                    continue
                to_run.append(spec)
            if to_run:
                runs = self.foundry.run_parallel(to_run, state) if op == OperationKind.PARALLEL_WORKSTREAMS else [self.foundry.run(s, state) for s in to_run]
            for run in runs:
                ledger.add_subagent()
                ledger.add_model_call(run.response)
                self._check_residency(state, run.response, "specialist")
                if run.report is not None and run.spec.independent and not run.report.blocked:
                    self._extract_disagreements(state, run, out, planner)
                if run.report is not None:
                    rep = run.report.model_dump(mode="json")
                    rep["_injection_flags"] = run.injection_flags
                    out.specialist_reports.append(rep)
                    self.tracer.emit("specialist", f"{run.spec.role} ({run.model}): {run.report.conclusion[:160]} conf={run.report.confidence:.2f}" + (" BLOCKED" if run.report.blocked else ""), data={"role": run.spec.role, "model": run.model, "independent": run.spec.independent, "confidence": run.report.confidence, "findings": len(run.report.findings), "blocked": run.report.blocked, "injection_flags": run.injection_flags, "turns": run.response.turns}, cost={"cost_usd": run.response.cost_usd, "input_tokens": run.response.input_tokens, "output_tokens": run.response.output_tokens})
                else:
                    out.errors.append(f"specialist {run.spec.role} failed ({run.response.error_kind or 'structural'}): {run.response.error[:200]}")
                    self.tracer.emit("failure", f"specialist {run.spec.role} failed: {run.response.error[:160]}", data={"error_kind": run.response.error_kind})
                    if run.response.error_kind == "transient":
                        ledger.add_retry()
        elif op == OperationKind.VERIFY:
            out.verification = self._verify(state, task, decision)
        elif op == OperationKind.SYNTHESIZE:
            out.synthesis = self._synthesize(state, beliefs, assessment)
        elif op == OperationKind.REQUEST_HUMAN_AUTHORIZATION:
            hr_spec = decision.human_request
            hr = HumanRequest(kind=hr_spec.kind if hr_spec else "decision", question=hr_spec.question if hr_spec else decision.rationale, why_not_inferable=hr_spec.why_not_inferable if hr_spec else "executive judged this non-inferable", options=list(hr_spec.options) if hr_spec else [], independent_work_remaining=bool(planner.compute_ready()))
            state.human_requests.append(hr)
            out.human_request = hr
            self.tracer.emit("blocked", f"human request: {hr.question[:160]}", data={"request_id": hr.id, "independent_work_remaining": hr.independent_work_remaining})
        elif op == OperationKind.WAIT_FOR_EXTERNAL_EVENT:
            kind = decision.wait_for_event_kind or "human_input"
            self.events.subscribe(state.mission_id, kind, affects={"tasks": [task.id] if task else []})
            out.waited_for = kind
            self.tracer.emit("blocked", f"waiting for event '{kind}'")
        elif op == OperationKind.COMPLETE_MISSION:
            engine = VerificationEngine(self.fabric, state)
            self._refresh_changed_artifacts(state, engine)
            gate = engine.mission_completion_check(state)
            self.tracer.emit("verify", f"completion gate: {gate.status.value} — {gate.summary[:200]}", data=gate.model_dump(mode="json"))
            if gate.status != VerificationStatus.PASSED:
                out.completion_refusal = gate.summary
            else:
                # R1: the deterministic gate passing is necessary, not sufficient. Before the
                # mission is declared done, a blind assessment of the same raw evidence has to
                # agree — and an open hold blocks completion regardless of what either side
                # would prefer. A passing anchor is additional evidence, never an action grant.
                blocked = self._anchor_before_completion(state, out)
                if blocked:
                    out.completion_refusal = blocked
                else:
                    out.completed = True
        return out

    def _executive_position(self, state: MissionState) -> str:
        if state.synthesis.get("conclusion"):
            return str(state.synthesis["conclusion"])
        leading = [h for h in state.hypotheses if h.status == "leading"] or sorted(state.hypotheses, key=lambda h: -h.confidence)
        if leading:
            return leading[0].statement
        # Never speak for the mission with a claim the world has moved past: a superseded
        # observation was true of an earlier moment and is not this mission's position now.
        top = sorted((c for c in state.claims if c.live()), key=lambda c: -(c.confidence * c.decision_relevance))
        return top[0].proposition if top else ""

    def _extract_disagreements(self, state: MissionState, run: Any, out: OperationOutcome, planner: Planner) -> None:
        """Independent cognition protocol: compare positions, keep material disagreements, target evidence."""
        from cogos.agent_foundry.foundry import DisagreementReport

        position = self._executive_position(state)
        if not position or run.report is None:
            return
        req = CognitionRequest(kind="challenge", system_prompt=PROMPTS["challenge"], prompt=f"QUESTION: {run.spec.objective}\n\nPOSITION A (executive): {position}\n\nPOSITION B (independent {run.spec.role}): {run.report.conclusion}\n\nFINDINGS B: {json.dumps([f.model_dump(mode='json') for f in run.report.findings], default=str)[:8000]}", schema_name="DisagreementReport", output_schema=schema_for(DisagreementReport), model=self.config.executive.model, mission_id=state.mission_id, metadata={"question": run.spec.objective, "executive_position": position, "specialist_position": run.report.conclusion})
        resp = self._cognition(state, req)
        if not resp.ok:
            return
        try:
            report = DisagreementReport.model_validate(resp.parsed)
        except Exception:  # noqa: BLE001
            return
        state.resources.setdefault("controller", {})["challenged"] = True
        material = [d for d in report.disagreements if d.material]
        out.disagreements = [d.model_dump(mode="json") for d in report.disagreements]
        self.tracer.emit("decision", f"independent challenge: {len(report.disagreements)} disagreement(s), {len(material)} material", data={"disagreements": out.disagreements, "executive_position": position[:300], "specialist_position": run.report.conclusion[:300]})
        for d in material[:3]:
            title = f"Resolve disagreement: {d.topic[:70]}"
            if any(t.title == title for t in state.tasks):
                continue
            planner.add_tasks_from_specs([TaskSpec(key=f"dis_{abs(hash(d.topic)) % 10_000}", title=title, description=d.resolution_plan or "gather discriminating primary evidence", operation_hint="instantiate_specialist", parameters_json=json.dumps({"role": "source_auditor", "objective": f"Discriminate between: (A) {d.executive_position[:200]} vs (B) {d.specialist_position[:200]}. {d.resolution_plan}", "tools": ["web_search", "web_fetch", "read_file"], "max_turns": 12}), priority=0.9, parallel_safe=True)])
            state.notes.append(f"material disagreement recorded: {d.topic[:120]}")

    def _calls_from_task(self, task: Task) -> list[Any]:
        from cogos.schemas.cognition import ToolCallSpec

        p = task.parameters
        calls = []
        if p.get("tool"):
            calls.append(ToolCallSpec(tool=p["tool"], arguments_json=json.dumps(p.get("arguments") or {}), purpose=task.title))
        for c in p.get("tool_calls") or []:
            calls.append(ToolCallSpec(tool=c.get("tool", ""), arguments_json=json.dumps(c.get("arguments") or {}), purpose=c.get("purpose", task.title)))
        return calls

    @staticmethod
    def _specs_from_task(task: Task) -> list[SpecialistSpec]:
        p = task.parameters
        raw = p.get("specialists") or ([p] if p.get("role") else [])
        specs = []
        for s in raw:
            specs.append(SpecialistSpec(role=str(s.get("role", "analyst")), objective=str(s.get("objective", task.title)), constraints=list(s.get("constraints") or []), tools=list(s.get("tools") or []), independent=bool(s.get("independent", False)), max_turns=int(s.get("max_turns", 12)), context_keys=list(s.get("context_keys") or ["claims", "evidence", "unknowns"])))
        return specs

    def _tool(self, state: MissionState, call: ToolCall, ledger: ResourceLedger, task: Optional[Task]) -> ToolResult:
        res = self.fabric.execute(call)
        ledger.add_tool_call(res)
        self._tool_history.append(res.ok)
        del self._tool_history[:-20]
        self.tracer.emit("tool_call", f"{call.tool} {'ok' if res.ok else 'FAILED'} ({res.duration_ms}ms) {res.error[:120]}", data={"tool": call.tool, "arguments": {k: (str(v)[:200]) for k, v in call.arguments.items()}, "ok": res.ok, "error_kind": res.error_kind, "action_class": res.verdict.action_class.value if res.verdict else "", "decision": res.verdict.decision.value if res.verdict else "", "injection_flags": res.injection_flags, "output_preview": res.output[:300]})
        if res.verdict and res.verdict.decision != PolicyDecision.ALLOW:
            blocked = BlockedOperation(operation=f"{call.tool} {json.dumps(call.arguments, default=str)[:200]}", action_class=res.verdict.action_class, reason=res.verdict.reason, what_would_unblock=("human authorization for action class '%s'" % res.verdict.action_class.value) if res.verdict.decision == PolicyDecision.REQUIRE_HUMAN else "policy change or an allowed alternative", task_id=task.id if task else None)
            state.blocked_operations.append(blocked)
            state.capability_state[call.tool] = {"last_verdict": res.verdict.decision.value, "reason": res.verdict.reason}
            self.tracer.emit("blocked", f"{call.tool}: {res.verdict.reason}", data={"action_class": res.verdict.action_class.value, "blocked_id": blocked.id, "executive_model": state.executive_model})
            if res.verdict.decision == PolicyDecision.REQUIRE_HUMAN:
                state.human_requests.append(HumanRequest(kind="authorization", question=f"Authorize {res.verdict.action_class.value} operation: {blocked.operation}", why_not_inferable="the action class requires explicit human authorization by policy", options=["authorize", "deny"], independent_work_remaining=True))
        return res

    def _anchor_before_completion(self, state: MissionState, out: Optional[OperationOutcome] = None) -> str:
        """Run (or honour) the reality anchor on the mission's headline position.

        Returns a refusal reason, or "" when nothing is holding completion. A hold from an
        earlier cycle is honoured here even if it was opened against a queued branch: this is
        the dispatch point for "declare the mission done".
        """
        for hold in state.open_holds():
            if hold.rounds < MAX_RESOLUTION_ROUNDS:
                self.anchors.attempt_resolution(state, hold)
        still_open = state.open_holds()
        if still_open:
            return "completion held by reality anchor: " + "; ".join(f"{h.kind.value}: {h.cause[:160]}" for h in still_open[:3])
        exhausted = [h for h in state.holds if h.status == HoldStatus.UNRESOLVED_EXHAUSTED and not h.resolution_receipt_id]
        if exhausted:
            # An exhausted dispute is reported as unresolved. Running out of rounds is not
            # agreement, so it does not become a pass.
            return "completion blocked: unresolved reality disagreement after the permitted resolution rounds: " + "; ".join(h.cause[:160] for h in exhausted[:3])

        if state.resources.get("anchor", {}).get("completion_assessment_id"):
            return ""  # already anchored for this completion attempt and cleared
        proposition = self._executive_position(state)
        try:
            outcome = self.anchors.run(
                state,
                question=f"From the observations alone, what do they establish about this mission's objective: {state.objective}",
                proposition=proposition,
                branch="mission",
                propositions=[c.description for c in state.success_criteria],
            )
        except BlindnessViolation as exc:
            # The packet could not be made blind — the executive's own conclusion is present in
            # the raw material. Failing closed is the only honest option: an anchor that cannot be
            # run independently has not agreed with anything, so completion is held rather than
            # granted, and the mission is told exactly why.
            self.tracer.emit(
                "anchor",
                f"HOLD — blind packet could not be constructed: {exc}",
                data={"reason": "blindness_violation", "detail": str(exc)[:400]},
            )
            state.notes.append(f"completion held: the reality anchor could not be run blindly ({exc})")
            return f"completion held: the reality anchor could not be run blindly — {exc}"
        if out is not None:
            out.anchor = outcome
        if outcome.held:
            return f"completion held by reality anchor: {outcome.summary()}"
        state.resources.setdefault("anchor", {})["completion_assessment_id"] = outcome.assessment.id
        return ""

    def _refresh_changed_artifacts(self, state: MissionState, engine: VerificationEngine) -> list[str]:
        """Re-verify artifacts whose bytes no longer match what was verified.

        Detecting a change is the completion gate's job; *responding* to one is the loop's.
        A regenerated file (a fix applied over a buggy first attempt) has to be checked again
        rather than accepted on its stale receipt or left to block the mission forever. A file
        that was deleted fails re-verification, which is the correct outcome.
        """
        from cogos.verification.engine import artifact_integrity

        refreshed: list[str] = []
        for a in state.artifacts:
            if not a.verified:
                continue
            ok, detail = artifact_integrity(a)
            if ok:
                continue
            self.tracer.emit("verify", f"artifact '{a.name}' changed since verification: {detail}", data={"artifact_id": a.id})
            engine.verify_artifact(a)
            refreshed.append(a.id)
            self._invalidate_criteria_resting_on(state, a)
        return refreshed

    def _invalidate_criteria_resting_on(self, state: MissionState, artifact: Any) -> None:
        """Un-satisfy criteria that were satisfied against an artifact that has since changed.

        Re-verifying the file establishes the *new* bytes. It says nothing about whether the
        criterion those old bytes satisfied is still met, so that judgement is withdrawn and has
        to be made again.
        """
        from cogos.verification.engine import TOKEN_OVERLAP_THRESHOLD, token_overlap

        for c in state.success_criteria:
            if not c.satisfied:
                continue
            method = (c.verification_method or "").lower()
            relates = token_overlap(c.description, f"{artifact.name} {artifact.summary}") >= TOKEN_OVERLAP_THRESHOLD
            if "artifact" not in method and not relates:
                continue
            c.satisfied = False
            c.verification_ids.clear()
            self.tracer.emit(
                "verify",
                f"criterion '{c.description[:80]}' withdrawn: it rested on artifact '{artifact.name}', which changed",
                data={"criterion_id": c.id, "artifact_id": artifact.id},
            )

    def _verify(self, state: MissionState, task: Optional[Task], decision: StepDecision) -> VerificationResult:
        engine = VerificationEngine(self.fabric, state)
        self._refresh_changed_artifacts(state, engine)
        result: Optional[VerificationResult] = None
        if task is not None:
            p = task.parameters
            if p.get("commands") or p.get("test_command") or p.get("verify_commands"):
                raw_cmds = p.get("commands") or p.get("verify_commands") or [p.get("test_command")]
                cmds: list[str] = [str(raw_cmds)] if isinstance(raw_cmds, str) else [str(c) for c in raw_cmds if c]
                before = len(self.fabric.call_log)
                tests_before = len(state.tests)
                result = engine.verify_code(cmds, cwd=p.get("cwd"), task_id=task.id)
                for res in self.fabric.call_log[before:]:
                    if res.error_kind in ("denied", "requires_human", "unavailable") and not any(b.operation.startswith(f"{res.tool} ") and not b.resolved for b in state.blocked_operations):
                        state.blocked_operations.append(BlockedOperation(operation=f"{res.tool} {json.dumps({'command': cmds}, default=str)[:160]}", action_class=res.verdict.action_class if res.verdict else ActionClass.REVERSIBLE_LOCAL, reason=res.error, what_would_unblock="enable the verification tool (shell/tests) in governance policy or provide an allowed alternative", task_id=task.id))
                        state.capability_state[res.tool] = {"last_verdict": res.error_kind, "reason": res.error}
                        self.tracer.emit("blocked", f"verification tool {res.tool} unavailable: {res.error}", data={"task_id": task.id})
                if p.get("expect_failure"):
                    # A reproduction step succeeds when the failure is observed. Only the
                    # verification result is inverted: the test ledger keeps the observed
                    # status, flagged so it is never mistaken for evidence the suite passes.
                    observed = result.status == VerificationStatus.FAILED
                    result.status = VerificationStatus.PASSED if observed else VerificationStatus.FAILED
                    result.summary = ("failure reproduced: " if observed else "failure did NOT reproduce: ") + result.summary
                    for rec in state.tests[tests_before:]:
                        rec.expected_failure = True
                        rec.summary = "(reproduction) " + rec.summary
            elif p.get("research"):
                relevant = [c.id for c in state.live_claims() if c.decision_relevance >= 0.5]
                result = engine.verify_research(relevant or None, task_id=task.id)
            elif task.artifact_ids:
                result = engine.verify_task(task)
            else:
                # Verify the most recent completed-but-unverified task instead.
                result = engine.verify_task(task)
            if result is not None:
                if result.id not in task.verification_attempt_ids:
                    task.verification_attempt_ids.append(result.id)
                cite(task.verification_ids, result, task.id)
        if result is None and not self._criteria_pass_useful(state, Planner(state), self._verification_pending(state)):
            # Nothing has changed since the last pass, or every open criterion is already
            # recorded undecidable: re-running it would only add dead records.
            result = VerificationResult(target_type="criteria", target_id=state.mission_id, status=VerificationStatus.INCONCLUSIVE, summary="criteria pass skipped: no change since the last pass", checks=[])
        if result is None:
            # Criteria pass: evaluate every unsatisfied criterion against verified state.
            checks = []
            for c in state.success_criteria:
                if c.satisfied:
                    continue
                res = engine.verify_criterion(c, evidence_ok=self._runtime_criterion_evidence(state, c))
                if res.status == VerificationStatus.INCONCLUSIVE:
                    # A criterion the deterministic checks cannot decide is resolved by an
                    # independent executive judgement over the evidence, not left to loop.
                    res = self._judge_criterion(state, c, res, engine)
                checks.append(res)
            state.resources.setdefault("controller", {})["criteria_checked_sig"] = self._criteria_sig(state)
            if checks:
                failed = [c for c in checks if c.status != VerificationStatus.PASSED]
                result = VerificationResult(target_type="criteria", target_id=state.mission_id, status=VerificationStatus.PASSED if not failed else VerificationStatus.INCONCLUSIVE, summary=f"{len(checks) - len(failed)}/{len(checks)} criteria verified; " + "; ".join(c.summary[:100] for c in failed), checks=[chk for c in checks for chk in c.checks])
            else:
                result = VerificationResult(target_type="criteria", target_id=state.mission_id, status=VerificationStatus.PASSED, summary="all criteria already satisfied", checks=[])
        # Criteria addressed by a verified task can now be checked.
        if task is not None and result.status == VerificationStatus.PASSED:
            for cid in task.addresses_criterion_ids:
                for c in state.success_criteria:
                    if c.id == cid and not c.satisfied:
                        engine.verify_criterion(c)
        if result.status == VerificationStatus.INCONCLUSIVE and result.checks and task is not None:
            result = self._judge_verification(state, task, result)
        self.tracer.emit("verify", f"{result.target_type}:{result.target_id} {result.status.value} — {result.summary[:160]}", data=result.model_dump(mode="json"))
        return result

    def _judge(self, state: MissionState, target: str, detail: str, result: VerificationResult, extra: Optional[dict[str, Any]] = None) -> Optional[Any]:
        """Ask the executive to judge deterministic checks it cannot decide. Returns a VerificationJudgment or None."""
        from cogos.schemas.cognition import VerificationJudgment

        checks = [c.model_dump(mode="json") for c in result.checks]
        prompt = f"MISSION: {state.objective}\n\nTARGET: {target}\n{detail}\n\nDETERMINISTIC CHECKS (already run by the runtime):\n{json.dumps(checks, default=str)[:8000]}"
        if extra:
            prompt += "\n\nSTATE EVIDENCE:\n" + json.dumps(extra, default=str)[:8000]
        prompt += "\n\nJudge whether the target meets its requirement. List exactly which properties you checked. Return the VerificationJudgment JSON."
        req = CognitionRequest(kind="verify", system_prompt=PROMPTS["verify"], prompt=prompt, schema_name="VerificationJudgment", output_schema=schema_for(VerificationJudgment), model=self.config.executive.model, mission_id=state.mission_id, metadata={"checks": checks, "target": target})
        try:
            resp = self._cognition(state, req)
        except ExecutiveUnavailable:
            return None
        if not resp.ok:
            return None
        try:
            return VerificationJudgment.model_validate(resp.parsed)
        except Exception:  # noqa: BLE001
            return None

    def _judge_verification(self, state: MissionState, task: Task, result: VerificationResult) -> VerificationResult:
        """Independent executive judgement over inconclusive deterministic checks (never over failed ones)."""
        judgment = self._judge(state, f"task '{task.title}' — {task.description}", f"RESULT SUMMARY: {task.result_summary[:500]}", result)
        if judgment is None:
            return result
        if judgment.status == "passed" and judgment.confidence >= 0.7 and judgment.checked:
            result.status = VerificationStatus.PASSED
            result.summary = f"executive judgement passed ({judgment.confidence:.2f}): {judgment.summary[:160]}; checked: {', '.join(judgment.checked[:5])}"
        elif judgment.status == "failed":
            result.status = VerificationStatus.FAILED
            result.summary = f"executive judgement failed: {judgment.summary[:160]}; issues: {'; '.join(judgment.issues[:3])}"
        return result

    def _judge_criterion(self, state: MissionState, criterion: Any, result: VerificationResult, engine: VerificationEngine) -> VerificationResult:
        """Resolve a criterion the deterministic checks cannot decide, so the loop converges.

        The judgement is bounded by the same rules as everywhere else: it may satisfy a criterion
        only with named checks and high confidence, and a failed judgement marks it unsatisfied.
        A repeated inconclusive judgement records the criterion as undecidable so the executive
        stops re-verifying it and either replans or reports it honestly.
        """
        extra = {
            "artifacts": [{"name": a.name, "path": a.path, "verified": a.verified, "summary": a.summary[:160]} for a in state.artifacts[-10:]],
            "tests": [{"name": t.name, "status": t.status.value, "summary": t.summary[:160]} for t in state.tests[-6:]],
            "claims": [{"proposition": c.proposition[:200], "status": c.status.value, "confidence": c.confidence, "independent_roots": c.source_independence} for c in sorted(state.live_claims(), key=lambda c: -c.decision_relevance)[:10]],
            "unresolved_contradictions": [c.description[:160] for c in state.unresolved_contradictions()],
            "completed_tasks": [t.title for t in state.completed_tasks()[-10:]],
        }
        judgment = self._judge(state, f"success criterion '{criterion.description}'", f"VERIFICATION METHOD: {criterion.verification_method or '(unspecified)'}", result, extra)
        controller = state.resources.setdefault("controller", {})
        undecidable: list[str] = controller.setdefault("undecidable_criteria", [])
        grounding = self._judgment_grounding(state, criterion)
        if judgment is not None and judgment.status == "passed" and not grounding:
            # F2: a judgement is an opinion about material, not a substitute for it. With no
            # artifact, test record, evidenced claim or tool call the runtime can point at,
            # a confident "I checked everything" attests to nothing and is not accepted.
            self.tracer.emit(
                "verify",
                f"ungrounded judgement rejected for criterion: {criterion.description[:100]}",
                data={"criterion_id": criterion.id, "checked": list(judgment.checked)[:5], "confidence": judgment.confidence},
            )
            result.status = VerificationStatus.INCONCLUSIVE
            result.checks.append(
                VerificationCheck(
                    name="judgement_grounding",
                    status=VerificationStatus.INCONCLUSIVE,
                    detail="executive reported the criterion met, but no artifact, test record, evidenced claim or tool call in mission state supports it",
                )
            )
            if criterion.id not in undecidable:
                undecidable.append(criterion.id)
            return result
        if judgment is None or judgment.status == "inconclusive" or (judgment.status == "passed" and not judgment.checked):
            if criterion.id in undecidable:
                result.summary = f"criterion '{criterion.description[:80]}' is not decidable from available evidence (recorded; will not be re-verified)"
                self.tracer.emit("verify", f"criterion undecidable: {criterion.description[:120]}", data={"criterion_id": criterion.id})
            else:
                undecidable.append(criterion.id)
            return result
        if judgment.status == "passed" and judgment.confidence >= 0.7:
            criterion.satisfied = True
            result.status = VerificationStatus.PASSED
            result.summary = f"criterion satisfied by executive judgement ({judgment.confidence:.2f}): {judgment.summary[:160]}; checked: {', '.join(judgment.checked[:5])}; grounded in: {'; '.join(grounding[:3])}"
            result.checks.append(VerificationCheck(name="judgement_grounding", status=VerificationStatus.PASSED, detail="; ".join(grounding[:5])))
            if criterion.id in undecidable:
                undecidable.remove(criterion.id)
            engine.record(result)
            cite(criterion.verification_ids, result, criterion.id, target_type="criterion")
        elif judgment.status == "failed":
            criterion.satisfied = False
            result.status = VerificationStatus.FAILED
            result.summary = f"criterion not met: {judgment.summary[:160]}; issues: {'; '.join(judgment.issues[:3])}"
        return result

    def _judgment_grounding(self, state: MissionState, criterion: Any) -> list[str]:
        """Material in mission state that an executive judgement about this criterion can rest on.

        Only things the runtime can independently re-check count: an artifact whose verified
        bytes are still on disk, a passed test record produced after the criterion existed, a
        supported claim carrying at least one evidence item, or a task addressing the criterion that
        carries its own passing receipt. Model text about its own diligence is not material.
        """
        from cogos.verification.engine import _id_timestamp_ms, _iso_to_ms, artifact_integrity, token_overlap

        found: list[str] = []
        created_ms = _id_timestamp_ms(criterion.id) or 0

        for a in state.artifacts:
            if not a.verified:
                continue
            ok, _ = artifact_integrity(a)
            if ok:
                found.append(f"artifact '{a.name}' at {a.path}")

        for t in state.tests:
            if t.status == VerificationStatus.PASSED and not t.expected_failure and (_iso_to_ms(t.ran_at) or -1) >= created_ms:
                found.append(f"passed test record '{t.name}'")

        for c in state.claims:
            if c.status not in (ClaimStatus.SUPPORTED, ClaimStatus.ESTABLISHED):
                continue
            if not any(state.evidence_item(eid) is not None for eid in c.evidence_for):
                continue
            if token_overlap(criterion.description, c.proposition) >= 0.2:
                found.append(f"evidenced claim {c.id}")

        for t in state.tasks:
            if criterion.id not in t.addresses_criterion_ids or t.status != TaskStatus.DONE:
                continue
            if state.passing_verifications(t.verification_ids, target_id=t.id):
                found.append(f"independently verified task '{t.title}'")

        return found

    def _runtime_criterion_evidence(self, state: MissionState, criterion: Any) -> Optional[bool]:
        """Deterministic checks the runtime can make on a criterion without a model.

        Returns True/False when the criterion is decidable from state, None otherwise.
        """
        vm = (criterion.verification_method or "").lower() + " " + criterion.description.lower()
        if "uncertaint" in vm or "unknowns" in vm:
            open_unknowns = state.open_unknowns()
            decision_changing = [u for u in open_unknowns if u.probability_changes_decision * u.decision_importance >= 0.5 and u.attempts == 0]
            listed = set(map(str, state.synthesis.get("remaining_uncertainties", []))) if state.synthesis else set()
            unbounded = [u for u in open_unknowns if u.probability_changes_decision * u.decision_importance >= 0.5 and u.question not in listed and u.attempts == 0]
            if state.unresolved_contradictions() and any(c.severity >= 0.5 for c in state.unresolved_contradictions()):
                return False
            return not decision_changing or not unbounded
        if ("decision" in vm or "conclusion" in vm) and ("evidence" in vm or "source" in vm):
            if not state.synthesis or not state.synthesis.get("conclusion"):
                return None
            supported = [c for c in state.live_claims() if c.status.value in ("supported", "established") and c.decision_relevance >= 0.5]
            serious = [c for c in state.unresolved_contradictions() if c.severity >= 0.5]
            if serious:
                return False
            return bool(supported) if supported else None
        return None

    def _synthesize(self, state: MissionState, beliefs: BeliefGraph, assessment: Assessment) -> Optional[Synthesis]:
        tournament = beliefs.tournament().model_dump(mode="json") if len(state.hypotheses) >= 2 else {}
        if tournament and tournament.get("leading_id"):
            for h in state.hypotheses:
                if h.id == tournament["leading_id"]:
                    tournament["leading_statement"] = h.statement
        claims = sorted(state.live_claims(), key=lambda c: -(c.confidence * c.decision_relevance))[:15]
        metadata = {
            "criteria": [{"id": c.id, "description": c.description, "satisfied": c.satisfied, "evidence": ", ".join(c.verification_ids)} for c in state.success_criteria],
            "claims": [{"id": c.id, "proposition": c.proposition, "confidence": c.confidence, "status": c.status.value, "decision_relevance": c.decision_relevance, "independence": c.source_independence} for c in claims],
            "hypotheses": [h.model_dump(mode="json") for h in state.hypotheses],
            "tournament": tournament,
            "contradictions": [c.model_dump(mode="json") for c in state.unresolved_contradictions()],
            "blocked": [b.model_dump(mode="json") for b in state.blocked_operations if not b.resolved],
            "unknowns": [{"id": u.id, "question": u.question, "priority": u.priority()} for u in state.open_unknowns()],
            "simulation": state.resources.get("last_simulation"),
            "decisions": [d.model_dump(mode="json") for d in state.decisions[-5:]],
            "tests": [t.model_dump(mode="json") for t in state.tests[-5:]],
        }
        prompt = "MISSION: " + state.objective + "\n\nSTATE FOR SYNTHESIS:\n" + json.dumps(metadata, default=str)[:30_000] + "\n\nReturn the Synthesis JSON."
        req = CognitionRequest(kind="synthesize", system_prompt=PROMPTS["synthesize"], prompt=prompt, schema_name="Synthesis", output_schema=schema_for(Synthesis), model=self.config.executive.model, effort="max" if assessment.stakes >= 0.5 else "high", timeout_seconds=self.config.executive.call_timeout_seconds, mission_id=state.mission_id, metadata=metadata)
        resp = self._cognition(state, req)
        if not resp.ok:
            self.tracer.emit("failure", f"synthesis failed: {resp.error[:160]}")
            return None
        try:
            syn = Synthesis.model_validate(resp.parsed)
        except Exception as exc:  # noqa: BLE001
            self.tracer.emit("failure", f"invalid synthesis: {exc}")
            return None
        # The runtime, not the model, owns criterion satisfaction: strip unverified assertions.
        for ca in syn.criteria_assessment:
            crit = next((c for c in state.success_criteria if c.id == ca.criterion_id), None)
            if crit is not None and ca.satisfied and not state.passing_verifications(crit.verification_ids):
                ca.satisfied = False
                ca.evidence = (ca.evidence + " [runtime: no verification record]").strip()
        state.synthesis = syn.model_dump(mode="json")
        state.synthesis["synthesised_at"] = iso_now()
        self.tracer.emit("operation", f"synthesis: {syn.conclusion[:160]} (conf {syn.confidence:.2f}, status {syn.mission_status})", data={"confidence": syn.confidence, "mission_status": syn.mission_status, "remaining_uncertainties": syn.remaining_uncertainties[:5]})
        if syn.mission_status == "blocked_external" and syn.blocked_by:
            state.blocked_operations.append(BlockedOperation(operation="mission completion", action_class=ActionClass.REVERSIBLE_EXTERNAL, reason=syn.blocked_by, what_would_unblock=syn.blocked_by))
        return syn

    # ------------------------------------------------------------------------------

    def _interpret(self, state: MissionState, decision: StepDecision, outcome: OperationOutcome, task: Optional[Task], beliefs: BeliefGraph, assessment: Assessment) -> ObservationInterpretation:
        op = decision.operation
        # Pure runtime operations need no model interpretation.
        if op in (OperationKind.COMPLETE_MISSION, OperationKind.WAIT_FOR_EXTERNAL_EVENT, OperationKind.REQUEST_HUMAN_AUTHORIZATION):
            return ObservationInterpretation(summary=f"{op.value} handled by runtime", progress_estimate=state.progress)
        if op == OperationKind.SYNTHESIZE:
            summary = "synthesis produced" if outcome.synthesis else "synthesis failed"
            interp = ObservationInterpretation(summary=summary, progress_estimate=state.progress)
            if task is not None:
                interp.task_updates.append(TaskUpdateSpec(task_id=task.id, status="done" if outcome.synthesis else "failed", result_summary=summary, failure_reason="" if outcome.synthesis else "synthesis unavailable", failure_kind="" if outcome.synthesis else "tool"))
            return interp
        if op == OperationKind.VERIFY:
            ver = outcome.verification
            interp = ObservationInterpretation(summary=f"verification {ver.status.value if ver else 'missing'}", progress_estimate=state.progress)
            if task is not None and ver is not None:
                passed = ver.status == VerificationStatus.PASSED
                blocked = ver.status == VerificationStatus.INCONCLUSIVE and any(not b.resolved and b.task_id == task.id for b in state.blocked_operations)
                status = "done" if passed else ("blocked" if blocked else "failed")
                interp.task_updates.append(TaskUpdateSpec(task_id=task.id, status=status, result_summary=ver.summary[:300], failure_reason="" if passed else ver.summary[:300], failure_kind="" if passed else ("tool" if blocked else "implementation")))
                if passed:
                    interp.new_evidence.append(EvidenceSpec(summary=f"Verification passed: {ver.summary[:200]}", source="tool:verification", kind="primary", reliability=0.95))
            return interp
        # Cognitive escalation (live-run area C): pick the cheapest mechanism that can honestly
        # handle this observation. Anything carrying judgment escalates; the mechanical residue
        # does not need a frontier deliberation to read it.
        escalation = classify(op, outcome, task, state)
        self.tracer.emit(
            "escalation",
            f"{op.value} -> {escalation.describe()[:200]}",
            data={"tier": escalation.tier.name, "reasons": escalation.reasons, "operation": op.value},
        )
        if escalation.tier is Tier.L0_DETERMINISTIC:
            return deterministic_interpretation(op, outcome, task, state)
        if escalation.tier is Tier.L1_DIGEST:
            digest = self._digest_observation(state, decision, outcome, task, assessment)
            if digest is not None:
                return digest_to_interpretation(digest, task, state)
            # Fall through to full interpretation rather than losing the observation.

        metadata = {
            "operation": op.value,
            "task_id": task.id if task else "",
            "task": self._task_view(task) if task else None,
            "task_resolves_unknowns": task.resolves_unknown_ids if task else [],
            "tool_results": [{"tool": r.tool, "ok": r.ok, "output": r.output[:4000], "error": r.error, "error_kind": r.error_kind, "injection_flags": r.injection_flags, "data": {k: v for k, v in r.data.items() if isinstance(v, (int, float, str, bool))}} for r in outcome.tool_results],
            "specialist_reports": outcome.specialist_reports,
            "reasoning_output": outcome.reasoning_output,
            "calculation_result": outcome.calculation_result,
            "calculation_error": outcome.calculation_error,
            "simulation_result": outcome.simulation_result,
            "errors": outcome.errors,
            "claims": [{"id": c.id, "proposition": c.proposition, "confidence": c.confidence} for c in state.live_claims()[:30]],
            "unknowns": [{"id": u.id, "question": u.question} for u in state.open_unknowns()[:10]],
            "hypotheses": [{"id": h.id, "statement": h.statement, "confidence": h.confidence} for h in state.hypotheses[:10]],
            "progress": state.progress,
        }
        prompt_obj = {k: v for k, v in metadata.items() if k not in ("tool_results",)}
        prompt = "MISSION: " + state.objective + "\n\nSTEP: " + decision.rationale + "\n\nRESULTS (structured):\n" + json.dumps(prompt_obj, default=str)[:24_000]
        if outcome.tool_results:
            prompt += "\n\nTOOL RESULTS (status only; content follows as untrusted blocks where external):\n" + json.dumps([{"tool": r.tool, "ok": r.ok, "error": r.error, "error_kind": r.error_kind, "trust": r.trust.value} for r in outcome.tool_results], default=str)
            trusted_outputs = [r for r in outcome.tool_results if r.trust != TrustLevel.UNTRUSTED_EXTERNAL and r.output]
            if trusted_outputs:
                prompt += "\n\nVERIFIED TOOL OUTPUT:\n" + "\n".join(f"[{r.tool}] {r.output[:3000]}" for r in trusted_outputs)
        prompt += "\n\nReturn the ObservationInterpretation JSON."
        untrusted = list(outcome.untrusted)
        for rep in outcome.specialist_reports:
            untrusted.append(wrap_untrusted(f"specialist:{rep.get('role')}", f"specialist:{rep.get('role')}", json.dumps(rep, default=str)[:12_000]))
        req = CognitionRequest(kind="interpret", system_prompt=PROMPTS["interpret"], prompt=prompt, schema_name="ObservationInterpretation", output_schema=schema_for(ObservationInterpretation), model=self.config.executive.model, untrusted=untrusted, effort=assessment.effort, timeout_seconds=self.config.executive.call_timeout_seconds, mission_id=state.mission_id, metadata=metadata)
        resp = self._cognition(state, req)
        if resp.ok:
            try:
                return ObservationInterpretation.model_validate(resp.parsed)
            except Exception as exc:  # noqa: BLE001
                self.tracer.emit("error", f"invalid interpretation: {exc}")
        else:
            self.tracer.emit("error", f"interpretation failed: {resp.error[:200]}")
        # Deterministic fallback keeps the loop alive.
        from cogos.adapters.scripted import HeuristicExecutive

        return ObservationInterpretation.model_validate(HeuristicExecutive()._interpret(req))

    def _digest_observation(self, state: MissionState, decision: StepDecision, outcome: OperationOutcome, task: Optional[Task], assessment: Assessment) -> Optional[Any]:
        """A bounded read of a routine observation (L1).

        The reduced schema is the mechanism: a digest structurally cannot revise beliefs, so an
        observation classified as not bearing on them cannot change them by accident, and cannot
        run up a 40k-token belief essay either.
        """
        results = [{"tool": r.tool, "ok": r.ok, "output": (r.output or "")[:3000], "trust": r.trust.value} for r in outcome.tool_results]
        prompt = (
            f"MISSION: {state.objective}\n\nSTEP: {decision.rationale[:400]}\n\n"
            f"TASK: {task.title if task else '(none)'}\n\n"
            f"OPEN UNKNOWNS: {json.dumps([{'id': u.id, 'question': u.question} for u in state.open_unknowns()[:5]], default=str)}\n\n"
            f"TOOL RESULTS:\n{json.dumps(results, default=str)[:12000]}\n\n"
            "Read the results and report what they show. Record only facts you can point at in the "
            "output above. Return the ObservationDigest JSON."
        )
        req = CognitionRequest(
            kind="digest",
            system_prompt=PROMPTS["digest"],
            prompt=prompt,
            schema_name="ObservationDigest",
            output_schema=schema_for(ObservationDigest),
            model=self.config.executive.model,
            untrusted=list(outcome.untrusted),
            effort="low",
            timeout_seconds=min(180, self.config.executive.call_timeout_seconds),
            mission_id=state.mission_id,
            metadata={"operation": decision.operation.value, "task_id": task.id if task else ""},
        )
        try:
            resp = self._cognition(state, req)
        except ExecutiveUnavailable:
            return None
        if not resp.ok:
            return None
        try:
            return ObservationDigest.model_validate(resp.parsed)
        except Exception:  # noqa: BLE001 - fall back to full interpretation
            return None

    def _apply_interpretation(self, state: MissionState, interp: ObservationInterpretation, outcome: OperationOutcome, task: Optional[Task], beliefs: BeliefGraph, world: WorldModelManager, planner: Planner) -> None:
        if interp.injection_detected or any(r.injection_flags for r in outcome.tool_results) or any(rep.get("_injection_flags") for rep in outcome.specialist_reports):
            flags = sorted({f for r in outcome.tool_results for f in r.injection_flags} | {f for rep in outcome.specialist_reports for f in rep.get("_injection_flags", [])})
            state.notes.append(f"injection attempt detected in cycle {state.usage.cycles}: {flags}")
            self.tracer.emit("blocked", f"injection attempt neutralised: {flags}", data={"flags": flags})
        for cs in interp.new_claims:
            beliefs.add_claim(Claim(proposition=cs.proposition, epistemic_status=cs.epistemic_status, confidence=_clamp(cs.confidence), decision_relevance=_clamp(cs.decision_relevance), falsification_conditions=list(cs.falsification_conditions), assumptions=list(cs.assumptions), provenance=Provenance(source=f"cycle:{state.usage.cycles}", trust=TrustLevel.SYSTEM, method=outcome.operation.value)))
        for es in interp.new_evidence:
            source = es.source or "unknown"
            trust = TrustLevel.VERIFIED_TOOL if source.startswith(("tool:", "calc:", "tests:")) else (TrustLevel.SPECIALIST if source.startswith("specialist:") else TrustLevel.UNTRUSTED_EXTERNAL)
            try:
                kind = EvidenceKind(es.kind)
            except ValueError:
                kind = EvidenceKind.SECONDARY
            ev = Evidence(summary=es.summary, supports_claim_ids=list(es.supports_claims), contradicts_claim_ids=list(es.contradicts_claims), kind=kind, provenance=Provenance(source=source, trust=trust, method=outcome.operation.value, reliability=_clamp(es.reliability) if es.reliability != 0.5 else 0.5, lineage=list(es.lineage), trace_id=None), content_excerpt=es.excerpt[:1000], supports_proposition=es.supports_proposition, scope=es.scope, freshness=es.freshness or None)
            beliefs.add_evidence(ev)
        for cu in interp.claim_updates:
            c = state.claim(cu.claim_id) or beliefs.find_claim(cu.claim_id)
            if c is None:
                continue
            if cu.new_confidence is not None:
                c.confidence = _clamp(cu.new_confidence)
            if cu.new_status:
                try:
                    from cogos.schemas.beliefs import ClaimStatus

                    c.status = ClaimStatus(cu.new_status)
                except ValueError:
                    pass
            c.updated_at = iso_now()
        for cs in interp.contradictions:
            # A contradiction settled by scope, definition or period is *resolved*, not replaced
            # by another record. Found live: the executive correctly reasoned that a "files do
            # not exist" claim and a "files exist" claim were each true of their own period, but
            # the only channel available appended a fresh severity-0 record while the original
            # severity-1.0 contradiction stayed open — so the controller re-issued must_falsify
            # every cycle against a dispute that had already been settled.
            if cs.resolves_contradiction_ids and cs.resolution.strip():
                for ctr_id in cs.resolves_contradiction_ids:
                    for existing_ctr in state.contradictions:
                        if existing_ctr.id == ctr_id and not existing_ctr.resolved:
                            existing_ctr.resolved = True
                            existing_ctr.resolution = cs.resolution[:600]
                            self.tracer.emit(
                                "verify",
                                f"contradiction {ctr_id} resolved ({cs.suspected_cause}): {cs.resolution[:160]}",
                                data={"contradiction_id": ctr_id, "cause": cs.suspected_cause, "resolution": cs.resolution[:400]},
                            )
                if not cs.claim_ids and not cs.description.strip():
                    continue
            ids = [cid for cid in cs.claim_ids if state.claim(cid)]
            unresolved_ids = [cid for cid in cs.claim_ids if not state.claim(cid)]
            description = cs.description
            if unresolved_ids:
                description = f"{description} [unresolved claim ids: {', '.join(unresolved_ids[:5])}]"
            if not ids and not description.strip():
                continue
            # Dedupe on claim ids when they resolve, otherwise on the normalised description.
            # Without this an unresolvable id appended a fresh contradiction every cycle,
            # which no completion gate could ever clear.
            key = tuple(sorted(ids)) if ids else ("desc", _normalise(cs.description))
            existing = {(tuple(sorted(c.claim_ids)) if c.claim_ids else ("desc", _normalise(c.description.split(" [unresolved claim ids:")[0]))) for c in state.contradictions}
            if key not in existing:
                state.contradictions.append(Contradiction(claim_ids=ids, description=description, severity=_clamp(cs.severity), suspected_cause=cs.suspected_cause or "unknown"))
        for hu in interp.hypothesis_updates:
            for h in state.hypotheses:
                if h.id == hu.hypothesis_id:
                    h.confidence = _clamp(hu.confidence)
                    if hu.status in ("active", "leading", "eliminated", "confirmed"):
                        h.status = hu.status
        for wu in interp.world_updates:
            try:
                world.apply(wu, Provenance(source=f"cycle:{state.usage.cycles}", trust=TrustLevel.SYSTEM, method=outcome.operation.value))
            except Exception as exc:  # noqa: BLE001
                self.tracer.emit("error", f"world update failed: {exc}")
        for cu2 in interp.causal_updates:
            try:
                world.apply_causal(cu2)
            except Exception as exc:  # noqa: BLE001
                self.tracer.emit("error", f"causal update failed: {exc}")
        for uid in interp.resolved_unknowns:
            for u in state.unknowns:
                if u.id == uid or u.question == uid:
                    u.resolved = True
                    u.resolution = interp.summary[:300]
        for us in interp.new_unknowns:
            if not any(_similar_text(u.question, us.question) for u in state.unknowns):
                state.unknowns.append(Unknown(question=us.question, decision_importance=_clamp(us.decision_importance), probability_changes_decision=_clamp(us.probability_changes_decision), expected_information_gain=_clamp(us.expected_information_gain), estimated_cost=max(0.05, us.estimated_cost)))
        for ls in interp.lessons:
            state.learned_lessons.append(Lesson(statement=ls, category="general"))
        for ls in interp.failure_lessons:
            state.learned_lessons.append(Lesson(statement=ls, category="failure"))
        for cid in interp.criteria_satisfied:
            for c in state.success_criteria:
                if c.id != cid:
                    continue
                # The runtime, not the model, owns criterion satisfaction: an assertion is
                # honoured only when a verification record for it actually passed.
                if state.passing_verifications(c.verification_ids):
                    c.satisfied = True
                else:
                    self.tracer.emit("verify", f"ignored unverified claim that criterion {cid} is satisfied", data={"criterion_id": cid, "verification_ids": c.verification_ids})
        # Task updates from the model, subject to runtime rules.
        seen_task_update = False
        for tu in interp.task_updates:
            t = state.task(tu.task_id)
            if t is None:
                continue
            if task is not None and t.id == task.id:
                seen_task_update = True
            self._apply_task_status(state, t, tu.status, tu.result_summary, tu.failure_reason, tu.failure_kind, planner)
        if task is not None and not seen_task_update:
            # Runtime default: tool/specialist failures mark the task failed; otherwise done.
            failed = [r for r in outcome.tool_results if not r.ok]
            blocked = [r for r in failed if r.error_kind in ("denied", "requires_human")]
            if blocked:
                self._apply_task_status(state, task, "blocked", "", blocked[0].error, "tool", planner)
            elif failed or (outcome.errors and not outcome.specialist_reports and not outcome.simulation_result and not outcome.reasoning_output):
                err = failed[0].error if failed else "; ".join(outcome.errors)
                kind = failed[0].error_kind if failed else "structural"
                self._apply_task_status(state, task, "failed", "", err, "transient" if kind == "transient" else "structural", planner)
            else:
                self._apply_task_status(state, task, "done", interp.summary[:300], "", "", planner)
        if interp.new_tasks:
            created = planner.add_tasks_from_specs(list(interp.new_tasks))
            if created:
                self.tracer.emit("operation", f"added {len(created)} task(s) from interpretation", data={"tasks": [t.title for t in created]})
        if outcome.simulation_result:
            state.resources["last_simulation"] = outcome.simulation_result
        for rep in outcome.specialist_reports:
            for a in rep.get("artifacts") or []:
                p = Path(str(a))
                if not p.is_absolute():
                    p = Path(self.config.repo_root) / p
                if p.exists() and p.is_file():
                    art = Artifact(name=p.name, kind="file", path=str(p), produced_by_task_id=task.id if task else None, summary=f"produced by specialist {rep.get('role')}")
                    state.artifacts.append(art)
                    if task is not None:
                        task.artifact_ids.append(art.id)
        beliefs.recompute()
        beliefs.detect_contradictions()

    def _apply_task_status(self, state: MissionState, t: Task, status: str, result_summary: str, failure_reason: str, failure_kind: str, planner: Planner) -> None:
        status = (status or "").lower()
        t.updated_at = iso_now()
        if status == "done":
            t.status = TaskStatus.DONE
            t.result_summary = result_summary or t.result_summary
            t.failure_reason = ""
            for uid in t.resolves_unknown_ids:
                for u in state.unknowns:
                    if u.id == uid:
                        u.attempts += 1
            self.tracer.emit("operation", f"task done: {t.title}", data={"task_id": t.id, "attempts": t.attempts})
        elif status == "failed":
            t.status = TaskStatus.FAILED
            t.failure_reason = failure_reason[:300]
            self.tracer.emit("failure", f"task failed: {t.title} — {failure_reason[:160]}", data={"task_id": t.id, "attempts": t.attempts, "failure_kind": failure_kind})
        elif status == "blocked":
            t.status = TaskStatus.BLOCKED
            t.failure_reason = failure_reason[:300]
            self.tracer.emit("blocked", f"task blocked: {t.title} — {failure_reason[:160]}", data={"task_id": t.id})
        elif status == "cancelled":
            t.status = TaskStatus.CANCELLED
        elif status in ("active", "pending"):
            t.status = TaskStatus.PENDING

    # ------------------------------------------------------------------------------

    def _attribute(self, state: MissionState, decision: StepDecision, outcome: OperationOutcome, task: Optional[Task], planner: Planner, interp: ObservationInterpretation) -> tuple[bool, str]:
        """Failure attribution, retry policy, and mission-level status transitions."""
        if task is not None and task.status == TaskStatus.FAILED:
            failed = [r for r in outcome.tool_results if not r.ok]
            err = task.failure_reason or (failed[0].error if failed else "; ".join(outcome.errors))
            kind = failed[0].error_kind if failed else ("transient" if any("(transient)" in e for e in outcome.errors) else "structural")
            rd = planner.retry_decision(task, err, kind)
            self.tracer.emit("retry" if rd.action == "retry" else "failure", f"{rd.action}: {rd.reason}", data={"task_id": task.id, "attempts": task.attempts, "failure_kind": kind, "signature": task.failure_signature})
            self.memory.remember(MemoryClass.FAILURE, f"Task '{task.title}' failed ({kind}): {err[:200]} -> {rd.action}", tags=["failure", task.operation_hint or "task"], mission_id=state.mission_id, confidence=0.8, importance=0.6)
            if rd.action == "retry":
                task.status = TaskStatus.PENDING
                state.usage.retries += 1
                if rd.backoff_seconds:
                    self._sleep(min(rd.backoff_seconds, 5.0))
            elif rd.action == "replan":
                if task.attempts < task.max_attempts:
                    self._replan_task(state, planner, task, err, kind)
                else:
                    self._cancel_dependents(state, task)
            elif rd.action == "abandon":
                self._cancel_dependents(state, task)
            elif rd.action == "escalate":
                task.status = TaskStatus.BLOCKED
        if outcome.completed:
            state.status = MissionStatus.COMPLETE
            state.timestamps.completed_at = iso_now()
            self.tracer.emit("complete", "mission complete: all completion gates passed", data={"progress": state.progress, "confidence": state.confidence})
            return True, "complete"
        if outcome.completion_refusal:
            state.notes.append(f"completion refused (cycle {state.usage.cycles}): {outcome.completion_refusal[:300]}")
            if planner.is_plan_exhausted():
                return self._replan(state, planner, outcome.completion_refusal)
        if outcome.human_request is not None and not outcome.human_request.independent_work_remaining:
            state.status = MissionStatus.BLOCKED_EXTERNAL
            state.notes.append(f"blocked_external: awaiting human: {outcome.human_request.question[:200]}")
            self.events.subscribe(state.mission_id, "human_input")
            return True, "awaiting human input"
        if outcome.waited_for:
            unresolved = [b for b in state.blocked_operations if not b.resolved]
            if unresolved:
                state.status = MissionStatus.BLOCKED_EXTERNAL
                state.notes.append("blocked_external: " + "; ".join(f"{b.operation[:60]} -> unblock: {b.what_would_unblock[:120]}" for b in unresolved[:3]))
            else:
                state.status = MissionStatus.PAUSED
                state.notes.append(f"waiting for event '{outcome.waited_for}'")
            return True, f"waiting for {outcome.waited_for}"
        if outcome.synthesis is not None and outcome.synthesis.mission_status == "blocked_external" and planner.is_plan_exhausted():
            state.status = MissionStatus.BLOCKED_EXTERNAL
            state.notes.append(f"blocked_external: {outcome.synthesis.blocked_by[:200]}")
            self.events.subscribe(state.mission_id, "human_input")
            return True, "blocked external"
        if planner.is_plan_exhausted() and not outcome.completed and decision.operation != OperationKind.COMPLETE_MISSION:
            # Nothing left to do: let the next cycle attempt synthesis/verification/completion.
            return False, ""
        return False, ""

    def _replan(self, state: MissionState, planner: Planner, refusal: str) -> tuple[bool, str]:
        attempts = int(state.resources.get("controller", {}).get("replans", 0))
        if attempts >= 3:
            unresolved_blocks = [b for b in state.blocked_operations if not b.resolved]
            if unresolved_blocks or state.unanswered_human_requests():
                state.status = MissionStatus.BLOCKED_EXTERNAL
                state.notes.append("blocked_external: replanning exhausted; " + "; ".join(b.what_would_unblock for b in unresolved_blocks)[:300])
                self.events.subscribe(state.mission_id, "human_input")
                return True, "blocked external"
            state.status = MissionStatus.FAILED
            state.notes.append(f"failed: completion gate unmet after {attempts} replans: {refusal[:200]}")
            self.tracer.emit("failure", "mission failed: replanning exhausted", data={"refusal": refusal})
            return True, "failed"
        state.resources.setdefault("controller", {})["replans"] = attempts + 1
        failed_sigs = [{"title": t.title, "reason": t.failure_reason, "signature": t.failure_signature} for t in state.failed_tasks()]
        metadata = {"refusal": refusal, "failed_tasks": failed_sigs, "criteria": [{"id": c.id, "description": c.description, "satisfied": c.satisfied, "verification_method": c.verification_method} for c in state.success_criteria], "blocked": [b.model_dump(mode="json") for b in state.blocked_operations if not b.resolved], "tests": [t.model_dump(mode="json") for t in state.tests[-5:]], "goals": [{"id": g.id, "title": g.title} for g in state.goals]}
        prompt = "MISSION: " + state.objective + "\n\nCOMPLETION GATE REFUSAL: " + refusal + "\n\nSTATE:\n" + json.dumps(metadata, default=str)[:20_000] + "\n\nReturn the Replan JSON."
        req = CognitionRequest(kind="replan", system_prompt=PROMPTS["replan"], prompt=prompt, schema_name="Replan", output_schema=schema_for(Replan), model=self.config.executive.model, timeout_seconds=self.config.executive.call_timeout_seconds, mission_id=state.mission_id, metadata=metadata)
        resp = self._cognition(state, req)
        plan: Optional[Replan] = None
        if resp.ok:
            try:
                plan = Replan.model_validate(resp.parsed)
            except Exception:  # noqa: BLE001
                plan = None
        if plan is None:
            plan = self._heuristic_replan(state, refusal)
        self.tracer.emit("decision", f"replan: {plan.rationale[:160]}", data={"new_tasks": [t.title for t in plan.new_tasks], "give_up": plan.give_up, "blocked_by": plan.blocked_by})
        if plan.new_tasks:
            created = planner.add_tasks_from_specs(plan.new_tasks)
            if created:
                return False, ""
        if plan.blocked_by:
            state.status = MissionStatus.BLOCKED_EXTERNAL
            state.blocked_operations.append(BlockedOperation(operation="mission completion", action_class=ActionClass.REVERSIBLE_EXTERNAL, reason=plan.blocked_by, what_would_unblock=plan.what_would_unblock or plan.blocked_by))
            state.notes.append(f"blocked_external: {plan.blocked_by[:200]}; unblock: {plan.what_would_unblock[:200]}")
            self.events.subscribe(state.mission_id, "human_input")
            return True, "blocked external"
        if plan.give_up:
            state.status = MissionStatus.FAILED
            state.notes.append(f"failed: {plan.rationale[:300]}")
            return True, "failed"
        return False, ""

    def _heuristic_replan(self, state: MissionState, refusal: str) -> Replan:
        low = refusal.lower()
        unresolved_blocks = [b for b in state.blocked_operations if not b.resolved]
        if unresolved_blocks:
            return Replan(rationale="completion blocked by operations outside the permitted action space", blocked_by="; ".join(b.reason for b in unresolved_blocks)[:300], what_would_unblock="; ".join(b.what_would_unblock for b in unresolved_blocks)[:300])
        if "test" in low and ("failed" in low or "fail" in low):
            done_fix = [t for t in state.tasks if t.title.startswith("Fix failing tests")]
            if len(done_fix) >= 2:
                return Replan(rationale="fix attempts exhausted", give_up=True)
            return Replan(rationale="tests are failing; diagnose and fix, then re-verify", new_tasks=[
                TaskSpec(key="fix", title=f"Fix failing tests (attempt {len(done_fix) + 1})", operation_hint="instantiate_specialist", parameters_json=json.dumps({"role": "debugger", "objective": "Diagnose and fix the failing tests without weakening them", "tools": ["read_file", "search_text", "write_file", "shell", "run_tests"], "max_turns": 40}), priority=0.9, parallel_safe=False),
                TaskSpec(key="reverify", title="Re-run verification after fix", operation_hint="verify", parameters_json=json.dumps({"commands": list(state.resources.get("required_tests") or ["python -m pytest -q"])}), depends_on=["fix"], priority=0.9, parallel_safe=False),
            ])
        if "criteria" in low or "criterion" in low:
            unmet = [c for c in state.success_criteria if not c.satisfied]
            tasks = []
            for i, c in enumerate(unmet[:3]):
                vm = c.verification_method.lower()
                if "test" in vm or "pytest" in vm:
                    tasks.append(TaskSpec(key=f"crit{i}", title=f"Run tests for criterion: {c.description[:60]}", operation_hint="verify", parameters_json=json.dumps({"commands": list(state.resources.get("required_tests") or ["python -m pytest -q"])}), priority=0.9, addresses_criteria=[c.description]))
                elif "evidence" in vm or "source" in vm:
                    tasks.append(TaskSpec(key=f"crit{i}", title=f"Gather verified evidence for: {c.description[:60]}", operation_hint="instantiate_specialist", parameters_json=json.dumps({"role": "source_auditor", "objective": f"Establish with independent primary sources: {c.description}", "tools": ["web_search", "web_fetch"], "max_turns": 15}), priority=0.85, addresses_criteria=[c.description]))
                    tasks.append(TaskSpec(key=f"critv{i}", title=f"Verify research for: {c.description[:60]}", operation_hint="verify", parameters_json=json.dumps({"research": True}), depends_on=[f"crit{i}"], priority=0.85, addresses_criteria=[c.description]))
            prior = int(state.resources.get("controller", {}).get("replans", 0))
            if tasks and prior <= 2:
                return Replan(rationale="unmet criteria need targeted verification work", new_tasks=tasks)
            return Replan(rationale="criteria cannot be verified with available capabilities", give_up=prior > 2, blocked_by="" if prior <= 2 else "no capability available to verify remaining criteria", what_would_unblock="access to a reasoning model or research tools to establish the remaining criteria")
        return Replan(rationale="no replanning heuristic applies", give_up=True)

    def _cancel_dependents(self, state: MissionState, task: Task) -> None:
        for t in state.tasks:
            if task.id in t.depends_on and t.status in (TaskStatus.PENDING, TaskStatus.READY, TaskStatus.BLOCKED):
                if t.operation_hint == "synthesize":
                    # Synthesis must always be able to run so the honest final state is explained.
                    t.depends_on = [d for d in t.depends_on if d != task.id]
                    continue
                t.status = TaskStatus.CANCELLED
                t.failure_reason = f"prerequisite {task.id} abandoned"

    MAX_REPLAN_DEPTH = 3
    MAX_REPLANS_PER_CHAIN = 5

    def _replan_task(self, state: MissionState, planner: Planner, task: Task, error: str, kind: str) -> None:
        """A structural failure needs a different strategy before the task is retried.

        Replanning is bounded twice over: by the depth of the prerequisite chain it creates and
        by the number of replans spent on one root task. Without both, each replacement task
        brought a fresh attempt budget and its own replan budget, so the plan never exhausted.
        """
        controller = state.resources.setdefault("controller", {})
        chain_counts: dict[str, int] = controller.setdefault("task_replans", {})
        root = str(task.parameters.get("_replan_root") or task.id)
        depth = int(task.parameters.get("_replan_depth", 0))
        spent = int(chain_counts.get(root, 0))
        if depth >= self.MAX_REPLAN_DEPTH or spent >= self.MAX_REPLANS_PER_CHAIN:
            reason = f"replanning exhausted for this branch (depth {depth}, {spent} replans on {root})"
            self.tracer.emit("failure", f"abandoning '{task.title[:60]}': {reason}", data={"task_id": task.id, "root": root, "depth": depth, "replans": spent})
            task.attempts = max(task.attempts, task.max_attempts)
            task.failure_reason = f"{task.failure_reason} | {reason}".strip(" |")[:300]
            self._cancel_dependents(state, task)
            return
        chain_counts[root] = spent + 1
        metadata = {"failed_task": self._task_view(task), "error": error[:500], "failure_kind": kind, "criteria": [{"id": c.id, "description": c.description, "satisfied": c.satisfied} for c in state.success_criteria], "tests": [t.model_dump(mode="json") for t in state.tests[-3:]], "blocked": [b.model_dump(mode="json") for b in state.blocked_operations if not b.resolved]}
        prompt = "MISSION: " + state.objective + "\n\nTASK FAILED STRUCTURALLY:\n" + json.dumps(metadata, default=str)[:12_000] + "\n\nPropose the minimal new tasks that change strategy (they will become prerequisites of the failed task, which is then retried), or explain what blocks it. Return the Replan JSON."
        req = CognitionRequest(kind="replan", system_prompt=PROMPTS["replan"], prompt=prompt, schema_name="Replan", output_schema=schema_for(Replan), model=self.config.executive.model, timeout_seconds=self.config.executive.call_timeout_seconds, mission_id=state.mission_id, metadata=metadata)
        plan: Optional[Replan] = None
        try:
            resp = self._cognition(state, req)
            if resp.ok:
                plan = Replan.model_validate(resp.parsed)
        except ExecutiveUnavailable:
            plan = None
        except Exception:  # noqa: BLE001
            plan = None
        if plan is None or (not plan.new_tasks and not plan.blocked_by and not plan.give_up):
            plan = self._heuristic_task_replan(state, task, error)
        self.tracer.emit("decision", f"replan after failure of '{task.title[:60]}': {plan.rationale[:140]}", data={"task_id": task.id, "new_tasks": [t.title for t in plan.new_tasks], "give_up": plan.give_up, "blocked_by": plan.blocked_by})
        if plan.new_tasks:
            created = planner.add_tasks_from_specs(plan.new_tasks)
            created_ids = [t.id for t in created if task.id not in t.depends_on]
            if created_ids:
                depth = int(task.parameters.get("_replan_depth", 0)) + 1
                for t in created:
                    t.parameters["_replan_depth"] = depth
                    t.parameters["_replan_root"] = str(task.parameters.get("_replan_root") or task.id)
                task.depends_on = sorted(set(task.depends_on + created_ids))
                task.status = TaskStatus.PENDING
                task.parameters["_strategy_note"] = f"strategy changed after: {error[:160]}"
                planner.compute_ready()
                return
        if plan.blocked_by:
            state.blocked_operations.append(BlockedOperation(operation=f"task {task.title[:80]}", action_class=ActionClass.REVERSIBLE_EXTERNAL, reason=plan.blocked_by[:300], what_would_unblock=(plan.what_would_unblock or plan.blocked_by)[:300], task_id=task.id))
            task.status = TaskStatus.BLOCKED
            return
        if plan.give_up:
            task.attempts = max(task.attempts, task.max_attempts)
            self._cancel_dependents(state, task)
            return
        task.status = TaskStatus.PENDING
        task.priority = max(0.1, task.priority - 0.15)

    def _heuristic_task_replan(self, state: MissionState, task: Task, error: str) -> Replan:
        low = error.lower()
        p = task.parameters
        n_prior = sum(1 for t in state.tasks if t.title.startswith(("Fix failing tests", "Strengthen evidence")))
        if task.operation_hint == "verify" and (p.get("commands") or p.get("test_command") or p.get("verify_commands")):
            if n_prior >= 2:
                return Replan(rationale="fix attempts exhausted for failing tests", give_up=True)
            return Replan(rationale="tests fail; a fix must precede re-verification", new_tasks=[TaskSpec(key="fix", title=f"Fix failing tests (attempt {n_prior + 1})", description=error[:300], operation_hint="instantiate_specialist", parameters_json=json.dumps({"role": "debugger", "objective": f"Diagnose and fix the failing tests without weakening them. Failure: {error[:300]}", "tools": ["read_file", "search_text", "write_file", "shell", "run_tests"], "max_turns": 40}), priority=0.95, parallel_safe=False)])
        if task.operation_hint == "verify" and p.get("research"):
            if n_prior >= 2:
                return Replan(rationale="evidence strengthening exhausted", give_up=True)
            weak = [c for c in state.claims if c.confidence >= 0.8 and c.source_independence < 2][:3]
            contested = [c for c in state.unresolved_contradictions()][:2]
            objective = "Find independent primary sources for: " + "; ".join(c.proposition[:120] for c in weak) if weak else "Resolve contradictions with primary sources: " + "; ".join(c.description[:120] for c in contested)
            return Replan(rationale="research verification failed; strengthen evidence independence and resolve contradictions", new_tasks=[TaskSpec(key="strengthen", title=f"Strengthen evidence (attempt {n_prior + 1})", description=error[:300], operation_hint="instantiate_specialist", parameters_json=json.dumps({"role": "source_auditor", "objective": objective, "tools": ["web_search", "web_fetch", "read_file"], "max_turns": 15}), priority=0.95)])
        if "no reasoning model" in low or "requires a model" in low:
            return Replan(rationale="task needs a reasoning model that is unavailable", blocked_by="reasoning model unavailable for specialist work", what_would_unblock="configure an executive adapter with model access")
        if task.attempts >= 2:
            return Replan(rationale="repeated structural failure with no alternative strategy", give_up=True)
        return Replan(rationale="retry with adjusted parameters", new_tasks=[])

    # ------------------------------------------------------------------------------

    def _learn(self, state: MissionState, decision: StepDecision, outcome: OperationOutcome, interp: ObservationInterpretation, task: Optional[Task]) -> None:
        cfg = self.config.memory
        significant = bool(task and task.status in (TaskStatus.DONE, TaskStatus.FAILED)) or outcome.completed or bool(outcome.synthesis)
        if significant:
            self.memory.remember(MemoryClass.EPISODIC, f"[{state.mission_id}] cycle {state.usage.cycles}: {decision.operation.value} -> {interp.summary[:220]}", tags=["episode", decision.operation.value], mission_id=state.mission_id, confidence=0.9, importance=0.5 if not outcome.completed else 0.8)
        for ls in interp.lessons:
            self.memory.remember(MemoryClass.PROCEDURAL if any(w in ls.lower() for w in ("use ", "prefer", "always", "never", "run ")) else MemoryClass.SEMANTIC, ls, tags=["lesson"], mission_id=state.mission_id, confidence=0.6, importance=0.55)
        for c in state.claims:
            if c.status.value == "established" and c.decision_relevance >= 0.6:
                self.memory.remember(MemoryClass.SEMANTIC, c.proposition, tags=["established", state.resources.get("mission_kind", "general")], mission_id=state.mission_id, confidence=c.confidence, importance=0.7, provenance=c.provenance)
        for link in state.world_model.causal_links:
            if link.confidence >= 0.7 and link.epistemic_status != EpistemicStatus.HYPOTHESIS:
                self.memory.remember(MemoryClass.CAUSAL, f"{link.cause} -> {link.effect} ({link.mechanism})", tags=["causal"], mission_id=state.mission_id, confidence=link.confidence, importance=0.6)
        if outcome.completed:
            self.memory.remember(MemoryClass.EPISODIC, f"Mission complete: {state.objective[:200]} — {str(state.synthesis.get('conclusion', ''))[:300]}", tags=["mission_complete", state.resources.get("mission_kind", "general")], mission_id=state.mission_id, confidence=0.9, importance=0.9)
            for d in state.decisions:
                if d.outcome_success is None and d.consequential:
                    DecisionJournal(self.store, state).resolve(d.decision_id, "mission completed with this decision in force", True)
        # Working memory: the active task state of this mission (scoped; dropped on completion).
        active = [t for t in state.tasks if t.status == TaskStatus.ACTIVE] or ([task] if task else [])
        if active:
            self.memory.remember(MemoryClass.WORKING, f"[{state.mission_id}] active: {'; '.join(t.title for t in active[:3])} (cycle {state.usage.cycles})", tags=["working"], mission_id=state.mission_id, confidence=1.0, importance=0.4)
        for rel in state.world_model.relations[-5:]:
            if rel.confidence >= 0.7:
                src = next((e.name for e in state.world_model.entities if e.id == rel.source_id), rel.source_id)
                dst = next((e.name for e in state.world_model.entities if e.id == rel.target_id), rel.target_id)
                self.memory.remember(MemoryClass.RELATIONAL, f"{src} --{rel.kind}--> {dst}", tags=["relation", rel.kind], mission_id=state.mission_id, confidence=rel.confidence, importance=0.5)
        for ev in state.evidence[-5:]:
            if ev.freshness:
                self.memory.remember(MemoryClass.TEMPORAL, f"{ev.summary[:160]} (valid as of {ev.freshness}; scope {ev.scope or 'unspecified'})", tags=["temporal"], mission_id=state.mission_id, confidence=ev.provenance.reliability, importance=0.45, valid_from=ev.freshness, provenance=ev.provenance)
                self.memory.remember(MemoryClass.META, f"source {ev.provenance.source}: reliability {ev.provenance.reliability:.2f}, kind {ev.kind.value}, roots {sorted(ev.root_sources())[:3]}", tags=["meta", "provenance"], mission_id=state.mission_id, confidence=0.8, importance=0.4)
        if state.usage.cycles % max(1, cfg.consolidation_interval_cycles) == 0:
            try:
                self.memory.expire()
                res = self.memory.consolidate(mission_id=state.mission_id)
                self.tracer.emit("learn", f"memory consolidation: {res}", data=res)
            except Exception as exc:  # noqa: BLE001
                self.tracer.emit("error", f"consolidation failed: {exc}")

    # ------------------------------------------------------------------------------

    def _journal_decision(self, state: MissionState, decision: StepDecision, assessment: Assessment) -> None:
        journal = DecisionJournal(self.store, state)
        conf = self.calibration.adjusted_confidence(str(state.resources.get("mission_kind", "general")), _clamp(decision.confidence))
        dec = Decision(objective=state.objective[:200], available_options=list(decision.alternatives_considered) or [decision.operation.value], selected_option=f"{decision.operation.value}: {decision.rationale[:200]}", concise_rationale=decision.rationale[:400], decisive_evidence=[e.id for e in state.evidence[-3:]], assumptions=[a.statement for a in state.assumptions if a.load_bearing][:5], expected_outcome=decision.expected_outcome[:300], confidence=conf, reversibility="reversible" if decision.operation not in (OperationKind.EXECUTE_ACTION,) else "partially_reversible", review_trigger="task outcome observed", consequential=True, domain=str(state.resources.get("mission_kind", "general")))
        journal.record(dec)
        self.tracer.emit("decision", f"{dec.selected_option[:160]} (conf {dec.confidence:.2f})", data={"decision_id": dec.decision_id, "options": dec.available_options, "assumptions": dec.assumptions})

    def _cognition(self, state: MissionState, req: CognitionRequest) -> CognitionResponse:
        resp = self.adapter.call(req)
        ResourceLedger(state.usage).add_model_call(resp)
        self.tracer.emit("operation", f"cognition:{req.kind} {'ok' if resp.ok else 'FAILED'} model={','.join(resp.models_used) or req.model} {resp.duration_ms}ms", data={"kind": req.kind, "ok": resp.ok, "error": resp.error[:200], "error_kind": resp.error_kind, "models_used": resp.models_used, "residency_ok": resp.residency_ok}, cost={"cost_usd": resp.cost_usd, "input_tokens": resp.input_tokens, "output_tokens": resp.output_tokens})
        if not resp.ok and resp.error_kind == "refused":
            # A provider safety classification narrows what this call can do; it never changes
            # the resident executive model and never justifies working around the safeguard.
            note = f"executive cognition '{req.kind}' was declined by provider safety classification; deterministic policy used for this step"
            if note not in state.notes:
                state.notes.append(note)
            state.capability_state[f"cognition:{req.kind}"] = {"last_verdict": "refused", "reason": resp.error[:300], "executive_model": state.executive_model}
            self.tracer.emit("blocked", f"cognition:{req.kind} declined by provider safety classification (model residency preserved)", data={"kind": req.kind, "error": resp.error[:300], "executive_model": state.executive_model})
        if req.kind in EXECUTIVE_KINDS:
            self._cognition_log.append((req.kind, resp))
            self._check_residency(state, resp, req.kind)
            if resp.ok and not resp.residency_ok:
                # Never accept a silently downgraded executive: treat as a failed call.
                return resp.model_copy(update={"ok": False, "error": f"model residency violation: requested {resp.model_requested}, served by {resp.models_used}", "error_kind": "unavailable"})
            if not resp.ok and resp.error_kind == "unavailable":
                raise ExecutiveUnavailable(resp.error)
        return resp

    def _check_residency(self, state: MissionState, resp: CognitionResponse, kind: str) -> None:
        if not resp.residency_ok:
            state.notes.append(f"residency violation ({kind}): requested {resp.model_requested}, served {resp.models_used}")
            self.tracer.emit("residency", f"{kind}: requested {resp.model_requested}, served {resp.models_used}", data={"requested": resp.model_requested, "served": resp.models_used})

    def _block_on_executive(self, state: MissionState, assessment: Assessment, error: str, cycle_no: int) -> CycleResult:
        state.status = MissionStatus.BLOCKED_EXTERNAL
        state.blocked_operations.append(BlockedOperation(operation="executive cognition", action_class=ActionClass.REVERSIBLE_EXTERNAL, reason=f"executive model unavailable: {error[:200]}", what_would_unblock=f"access to executive model '{state.executive_model}' (no downgrade is performed)"))
        state.notes.append(f"blocked_external: executive model unavailable ({error[:160]}); model residency preserved")
        self.tracer.emit("blocked", f"executive unavailable: {error[:160]}", cycle=cycle_no, data={"executive_model": state.executive_model})
        return CycleResult(cycle_no, None, None, assessment, stop=True, stop_reason="executive unavailable")

    DENIAL_ANSWERS = ("deny", "denied", "no", "refuse", "refused", "reject", "rejected", "do not", "don't")

    def _apply_grants(self, state: MissionState) -> None:
        for g in state.permissions.get("grants", []) or []:
            self.fabric.firewall.grant(str(g))
        # Unblock tasks whose blocked operation is now authorised. Retry attempts stay bounded:
        # a reactivated task that fails again still runs through retry_decision.
        for b in state.blocked_operations:
            if b.resolved or b.action_class.value not in self.fabric.firewall.human_grants:
                continue
            b.resolved = True
            t = state.task(b.task_id) if b.task_id else None
            if t and t.status == TaskStatus.BLOCKED:
                if t.attempts >= t.max_attempts:
                    t.status = TaskStatus.FAILED
                    t.failure_reason = f"authorized after {t.attempts} attempts, but the attempt budget is exhausted"
                else:
                    t.status = TaskStatus.PENDING
            for hr in state.human_requests:
                if not hr.answered and b.action_class.value in hr.question:
                    hr.answered = True
                    hr.answer = "authorized"

    def _record_denied_authorization(self, state: MissionState) -> None:
        """An explicit refusal permanently closes the blocked operation and its task."""
        for hr in state.human_requests:
            if not hr.answered or hr.kind != "authorization":
                continue
            if not any(hr.answer.strip().lower().startswith(d) for d in self.DENIAL_ANSWERS):
                continue
            for b in state.blocked_operations:
                if b.resolved or b.action_class.value not in hr.question:
                    continue
                b.resolved = True
                b.what_would_unblock = f"denied by the human ({hr.answer[:80]}); this operation will not be retried"
                t = state.task(b.task_id) if b.task_id else None
                if t and t.status in (TaskStatus.BLOCKED, TaskStatus.PENDING, TaskStatus.READY):
                    t.status = TaskStatus.CANCELLED
                    t.failure_reason = "human denied the required authorization"
                    self._cancel_dependents(state, t)
                    self.tracer.emit("blocked", f"authorization denied; cancelled task '{t.title[:80]}'", data={"task_id": t.id, "action_class": b.action_class.value})

    def _scenario_from_state(self, state: MissionState) -> dict[str, Any]:
        """Build a scenario from hypotheses when the executive supplied none (scenario analysis, not fake precision)."""
        options = []
        hyps = [h for h in state.hypotheses if h.status != "eliminated"] or state.hypotheses
        for h in hyps[:4]:
            p = _clamp(h.confidence)
            options.append({"name": h.statement[:60], "description": h.statement, "outcomes": [{"name": "holds", "probability": p, "benefit": 1.0, "cost": 0.5, "risk": 0.1}, {"name": "fails", "probability": 1 - p, "benefit": 0.0, "cost": 0.5, "risk": 0.4}], "assumptions": {"evidence_strength": 1.0}, "information_missing": [u.question for u in state.open_unknowns()[:3]]})
        if not options:
            options = [{"name": "proceed", "outcomes": [{"name": "ok", "probability": 0.5, "benefit": 1.0, "cost": 0.5}]}, {"name": "do_not_proceed", "outcomes": [{"name": "ok", "probability": 1.0, "benefit": 0.0, "cost": 0.0}]}]
        return {"question": state.objective[:200], "options": options, "seed": 7, "samples": 1000, "assumption_ranges": {"evidence_strength": [0.5, 1.5]}}

    def _mission_confidence(self, state: MissionState) -> float:
        crit = state.success_criteria
        sat = sum(1 for c in crit if c.satisfied) / len(crit) if crit else 0.0
        ctr_pen = min(0.5, 0.2 * len(state.unresolved_contradictions()))
        syn_conf = float(state.synthesis.get("confidence", 0.0)) if state.synthesis else 0.0
        return round(_clamp(0.5 * sat + 0.3 * syn_conf + 0.2 * state.progress - ctr_pen), 3)

    # ------------------------------------------------------------------------------

    def _persist(self, state: MissionState, result: CycleResult) -> None:
        payload = {"cycle": result.cycle, "operation": result.decision.operation.value if result.decision else None, "stop": result.stop, "reason": result.stop_reason, "status": state.status.value}
        self.store.save_mission(state, "cycle", payload)
        every = max(1, self.config.checkpoint_every_cycles)
        if state.usage.cycles % every == 0 or result.stop:
            self._checkpoint(state)

    def _checkpoint(self, state: MissionState, final: bool = False) -> None:
        # R3: the versioned handoff travels with every checkpoint, so a resumed run reconciles
        # against pointers into durable state rather than trusting a prose summary of it.
        try:
            state.resources["handoff"] = handoff(state, next_action=state.resources.get("next_action", ""))
            state.resources["continuity"] = self.continuity.model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001 - a handoff failure must not kill the run
            self.tracer.emit("error", f"handoff export failed: {exc}")
        try:
            path = self.store.export_snapshot(state.mission_id, self.config.snapshots_dir)
            state.timestamps.last_checkpoint_at = iso_now()
            self.tracer.emit("checkpoint", f"snapshot {path.name}{' (final)' if final else ''}", data={"path": str(path), "status": state.status.value})
        except Exception as exc:  # noqa: BLE001 - checkpoint failure must not kill the run
            self.tracer.emit("error", f"checkpoint failed: {exc}")
        try:
            self.store.save_mission(state, "checkpoint")
        except Exception as exc:  # noqa: BLE001
            self.tracer.emit("error", f"checkpoint save failed: {exc}")


def _normalise(text: str) -> str:
    return " ".join(text.lower().split())


def _clamp(x: Any) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.5


def _fact(statement: str, source: str) -> Any:
    from cogos.schemas.mission import Fact

    return Fact(statement=statement, epistemic_status=EpistemicStatus.OBSERVATION, provenance=Provenance(source=source, trust=TrustLevel.HUMAN_PRINCIPAL, reliability=0.95))


def _similar_text(a: str, b: str) -> bool:
    ta = {w for w in "".join(ch.lower() if ch.isalnum() else " " for ch in a).split() if len(w) > 2}
    tb = {w for w in "".join(ch.lower() if ch.isalnum() else " " for ch in b).split() if len(w) > 2}
    return bool(ta and tb) and len(ta & tb) / len(ta | tb) >= 0.6
