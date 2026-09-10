"""Mission state — the source of truth for a mission."""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from cogos.ids import iso_now, new_id
from cogos.schemas.anchor import (
    AnchorAssessment,
    BeliefSnapshot,
    BranchHold,
    EvidenceSnapshot,
    HoldStatus,
    Observation,
    RealityDisagreement,
    ResolutionReceipt,
)
from cogos.schemas.beliefs import Claim, Contradiction, Evidence, Hypothesis
from cogos.schemas.common import ActionClass, EpistemicStatus, Provenance, VerificationStatus
from cogos.schemas.decisions import Decision
from cogos.schemas.verification import VerificationResult
from cogos.schemas.world import WorldModel


class MissionStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED_EXTERNAL = "blocked_external"
    COMPLETE = "complete"
    FAILED = "failed"
    ABANDONED = "abandoned"


class TaskStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class SuccessCriterion(BaseModel):
    id: str = Field(default_factory=lambda: new_id("sc"))
    description: str
    verification_method: str = Field(default="", description="How satisfaction is checked (test, artifact, evidence)")
    satisfied: bool = False
    verification_ids: list[str] = Field(default_factory=list)
    explicit: bool = Field(default=True, description="Stated by the human (True) or inferred (False)")


class Fact(BaseModel):
    id: str = Field(default_factory=lambda: new_id("fct"))
    statement: str
    epistemic_status: EpistemicStatus = EpistemicStatus.OBSERVATION
    provenance: Optional[Provenance] = None
    claim_id: Optional[str] = None


class Assumption(BaseModel):
    id: str = Field(default_factory=lambda: new_id("asm"))
    statement: str
    rationale: str = ""
    load_bearing: bool = Field(default=False, description="Would the conclusion change if false?")
    validated: Optional[bool] = None
    validation_method: str = ""


class Unknown(BaseModel):
    id: str = Field(default_factory=lambda: new_id("unk"))
    question: str
    decision_importance: float = Field(default=0.5, ge=0.0, le=1.0)
    probability_changes_decision: float = Field(default=0.5, ge=0.0, le=1.0)
    expected_information_gain: float = Field(default=0.5, ge=0.0, le=1.0)
    estimated_cost: float = Field(default=0.3, ge=0.0, description="Relative cost incl. latency and risk")
    resolved: bool = False
    resolution: str = ""
    resolving_evidence_ids: list[str] = Field(default_factory=list)
    attempts: int = 0

    def priority(self) -> float:
        """Practical approximation of expected decision value per unit cost."""
        gain = self.expected_information_gain * self.probability_changes_decision * self.decision_importance
        return gain / max(self.estimated_cost, 0.05)


class Goal(BaseModel):
    id: str = Field(default_factory=lambda: new_id("goal"))
    title: str
    level: str = Field(default="strategic", description="strategic|milestone|workstream")
    parent_id: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING
    success_criterion_ids: list[str] = Field(default_factory=list)
    rationale: str = ""


class Task(BaseModel):
    id: str = Field(default_factory=lambda: new_id("task"))
    title: str
    description: str = ""
    goal_id: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING
    depends_on: list[str] = Field(default_factory=list)
    operation_hint: str = Field(default="", description="Preferred operation kind if known")
    parameters: dict[str, Any] = Field(default_factory=dict)
    priority: float = Field(default=0.5, ge=0.0, le=1.0)
    parallel_safe: bool = True
    attempts: int = 0
    max_attempts: int = 3
    result_summary: str = ""
    failure_reason: str = ""
    failure_signature: str = Field(default="", description="Structural signature of the last failure to avoid identical retries")
    artifact_ids: list[str] = Field(default_factory=list)
    verification_ids: list[str] = Field(default_factory=list)
    verification_attempt_ids: list[str] = Field(
        default_factory=list,
        description="Every verification run against this task, passing or not. Distinct from "
        "verification_ids (passing receipts only) so 'was it checked?' and 'did it pass?' "
        "stay separate questions.",
    )
    created_at: str = Field(default_factory=iso_now)
    updated_at: str = Field(default_factory=iso_now)
    resolves_unknown_ids: list[str] = Field(default_factory=list)
    addresses_criterion_ids: list[str] = Field(default_factory=list)


class Commitment(BaseModel):
    id: str = Field(default_factory=lambda: new_id("cmt"))
    statement: str
    made_at: str = Field(default_factory=iso_now)
    fulfilled: bool = False


class Risk(BaseModel):
    id: str = Field(default_factory=lambda: new_id("risk"))
    description: str
    probability: float = Field(default=0.3, ge=0.0, le=1.0)
    impact: float = Field(default=0.5, ge=0.0, le=1.0)
    mitigation: str = ""
    materialized: bool = False


class BlockedOperation(BaseModel):
    id: str = Field(default_factory=lambda: new_id("blk"))
    operation: str
    action_class: ActionClass
    reason: str
    what_would_unblock: str
    task_id: Optional[str] = None
    blocked_at: str = Field(default_factory=iso_now)
    resolved: bool = False


class Artifact(BaseModel):
    id: str = Field(default_factory=lambda: new_id("art"))
    name: str
    kind: str = Field(default="file", description="file|report|dataset|code|decision|test_result")
    path: Optional[str] = None
    content_hash: Optional[str] = None
    summary: str = ""
    produced_by_task_id: Optional[str] = None
    verified: bool = False
    verified_hash: Optional[str] = Field(
        default=None,
        description="sha256 of the bytes that were actually verified. The completion gate re-hashes "
        "the file and refuses to accept an artifact whose content no longer matches this value.",
    )
    verified_at: Optional[str] = None
    created_at: str = Field(default_factory=iso_now)


class TestRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("tst"))
    name: str
    command: str = ""
    status: VerificationStatus = VerificationStatus.SKIPPED
    summary: str = ""
    ran_at: Optional[str] = None
    task_id: Optional[str] = None
    expected_failure: bool = Field(
        default=False,
        description="A reproduction run: a FAILED status here is the desired observation, so this record is never evidence that tests pass.",
    )


class Lesson(BaseModel):
    id: str = Field(default_factory=lambda: new_id("lsn"))
    statement: str
    category: str = Field(default="general", description="general|tool|strategy|domain|failure")
    evidence_ids: list[str] = Field(default_factory=list)
    reusable: bool = False


class CandidateSkill(BaseModel):
    id: str = Field(default_factory=lambda: new_id("cskill"))
    name: str
    description: str
    procedure: list[str] = Field(default_factory=list)
    source_trajectory_ids: list[str] = Field(default_factory=list)
    status: str = Field(default="candidate", description="candidate|evaluating|promoted|rejected")
    evaluation_summary: str = ""


class HumanRequest(BaseModel):
    id: str = Field(default_factory=lambda: new_id("hreq"))
    kind: str = Field(description="authorization|credential|decision|information")
    question: str
    why_not_inferable: str
    options: list[str] = Field(default_factory=list)
    independent_work_remaining: bool = True
    answered: bool = False
    answer: str = ""
    created_at: str = Field(default_factory=iso_now)


class ResourceUsage(BaseModel):
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    retries: int = 0
    subagents_spawned: int = 0
    network_requests: int = 0
    wall_clock_seconds: float = 0.0
    estimated_cost_usd: float = 0.0
    cycles: int = 0


class Budget(BaseModel):
    max_cycles: int = 200
    max_model_calls: int = 400
    max_subagents: int = 20
    max_cost_usd: Optional[float] = None
    max_wall_clock_seconds: Optional[float] = None


class Timestamps(BaseModel):
    created_at: str = Field(default_factory=iso_now)
    updated_at: str = Field(default_factory=iso_now)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    last_checkpoint_at: Optional[str] = None


class MissionState(BaseModel):
    """The durable aggregate root. Conversation history is never the source of truth."""

    mission_id: str = Field(default_factory=lambda: new_id("msn"))
    schema_version: int = 1
    objective: str
    status: MissionStatus = MissionStatus.DRAFT
    success_criteria: list[SuccessCriterion] = Field(default_factory=list)
    inferred_constraints: list[str] = Field(default_factory=list)
    explicit_constraints: list[str] = Field(default_factory=list)
    permissions: dict[str, Any] = Field(default_factory=dict)
    resources: dict[str, Any] = Field(default_factory=dict)
    known_facts: list[Fact] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    unknowns: list[Unknown] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    goals: list[Goal] = Field(default_factory=list)
    tasks: list[Task] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    commitments: list[Commitment] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    blocked_operations: list[BlockedOperation] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    tests: list[TestRecord] = Field(default_factory=list)
    verifications: list[VerificationResult] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list, description="Raw material captured by the kernel collector (R1)")
    evidence_snapshots: list[EvidenceSnapshot] = Field(default_factory=list)
    belief_snapshots: list[BeliefSnapshot] = Field(default_factory=list)
    anchor_assessments: list[AnchorAssessment] = Field(default_factory=list)
    disagreements: list[RealityDisagreement] = Field(default_factory=list)
    holds: list[BranchHold] = Field(default_factory=list)
    resolution_receipts: list[ResolutionReceipt] = Field(default_factory=list)
    learned_lessons: list[Lesson] = Field(default_factory=list)
    candidate_skills: list[CandidateSkill] = Field(default_factory=list)
    human_requests: list[HumanRequest] = Field(default_factory=list)
    world_model: WorldModel = Field(default_factory=WorldModel)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    executive_model: str = Field(default="", description="Resident executive model id")
    capability_state: dict[str, Any] = Field(default_factory=dict)
    permission_state: dict[str, Any] = Field(default_factory=dict)
    usage: ResourceUsage = Field(default_factory=ResourceUsage)
    budget: Budget = Field(default_factory=Budget)
    timestamps: Timestamps = Field(default_factory=Timestamps)
    synthesis: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    version: int = Field(default=0, description="Optimistic-concurrency version in the store")

    # --- convenience accessors -------------------------------------------------

    def task(self, task_id: str) -> Optional[Task]:
        for t in self.tasks:
            if t.id == task_id:
                return t
        return None

    def claim(self, claim_id: str) -> Optional[Claim]:
        for c in self.claims:
            if c.id == claim_id:
                return c
        return None

    def verification(self, verification_id: str) -> Optional[VerificationResult]:
        for v in self.verifications:
            if v.id == verification_id:
                return v
        return None

    def passing_verifications(
        self,
        ids: list[str],
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
    ) -> list[VerificationResult]:
        """Resolve ids to records that actually passed *for the stated target*.

        Unknown ids resolve to nothing. When `target_type`/`target_id` are supplied the record
        must also be bound to that exact target: a passing receipt for some other artifact is
        not evidence that this criterion was verified, however it came to be cited.
        """
        out = []
        for vid in ids:
            v = self.verification(vid)
            if v is None or v.status != VerificationStatus.PASSED:
                continue
            if target_type is not None and v.target_type != target_type:
                continue
            if target_id is not None and v.target_id != target_id:
                continue
            out.append(v)
        return out

    def referenced_verification_ids(self) -> set[str]:
        """Every verification id some part of durable state still points at (F5 retention)."""
        referenced: set[str] = set()
        for c in self.success_criteria:
            referenced.update(c.verification_ids)
        for t in self.tasks:
            referenced.update(t.verification_ids)
            referenced.update(t.verification_attempt_ids)
        return referenced

    def open_holds(self) -> list[BranchHold]:
        return [h for h in self.holds if h.status == HoldStatus.OPEN]

    def observation(self, observation_id: str) -> Optional[Observation]:
        for o in self.observations:
            if o.id == observation_id:
                return o
        return None

    def evidence_snapshot(self, snapshot_id: str) -> Optional[EvidenceSnapshot]:
        for s in self.evidence_snapshots:
            if s.id == snapshot_id:
                return s
        return None

    def evidence_item(self, evidence_id: str) -> Optional[Evidence]:
        for e in self.evidence:
            if e.id == evidence_id:
                return e
        return None

    def active_tasks(self) -> list[Task]:
        return [t for t in self.tasks if t.status in (TaskStatus.ACTIVE, TaskStatus.READY)]

    def completed_tasks(self) -> list[Task]:
        return [t for t in self.tasks if t.status == TaskStatus.DONE]

    def failed_tasks(self) -> list[Task]:
        return [t for t in self.tasks if t.status == TaskStatus.FAILED]

    def open_unknowns(self) -> list[Unknown]:
        return [u for u in self.unknowns if not u.resolved]

    def unresolved_contradictions(self) -> list[Contradiction]:
        return [c for c in self.contradictions if not c.resolved]

    def unanswered_human_requests(self) -> list[HumanRequest]:
        return [h for h in self.human_requests if not h.answered]

    def touch(self) -> None:
        self.timestamps.updated_at = iso_now()
