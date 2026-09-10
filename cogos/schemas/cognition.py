"""Structured-output contracts for every call to the executive model.

These are the *only* shapes the executive model is asked to produce. Keeping
cognition behind typed contracts lets the deterministic runtime validate,
persist, and replay every cognitive step, and lets a scripted adapter stand in
for the model in tests and evaluations.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from cogos.schemas.common import EpistemicStatus, OperationKind


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object string leniently; non-objects and errors yield {}."""
    import json

    if not text:
        return {}
    try:
        val = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return val if isinstance(val, dict) else {}


class CriterionSpec(BaseModel):
    description: str
    verification_method: str = ""
    explicit: bool = True


class UnknownSpec(BaseModel):
    question: str
    decision_importance: float = 0.5
    probability_changes_decision: float = 0.5
    expected_information_gain: float = 0.5
    estimated_cost: float = 0.3


class HypothesisSpec(BaseModel):
    question: str
    statement: str
    prior: float = 0.5
    unique_predictions: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class TaskSpec(BaseModel):
    key: str = Field(description="Short unique key used for dependency references within this plan")
    title: str
    description: str = ""
    goal_key: str = ""
    depends_on: list[str] = Field(default_factory=list, description="Keys of prerequisite tasks")
    operation_hint: str = Field(default="", description="One of the OperationKind values, if known")
    parameters_json: str = Field(default="{}", description="JSON object of task parameters (tool, arguments, paths, commands)")
    priority: float = 0.5
    parallel_safe: bool = True
    resolves_unknowns: list[str] = Field(default_factory=list, description="Unknown questions this task addresses")
    addresses_criteria: list[str] = Field(default_factory=list, description="Criterion descriptions this task advances")


class GoalSpec(BaseModel):
    key: str
    title: str
    level: str = "strategic"
    parent_key: str = ""
    rationale: str = ""


class RiskSpec(BaseModel):
    description: str
    probability: float = 0.3
    impact: float = 0.5
    mitigation: str = ""


class HumanRequestSpec(BaseModel):
    kind: str = Field(description="authorization|credential|decision|information")
    question: str
    why_not_inferable: str
    options: list[str] = Field(default_factory=list)


class MissionCompilation(BaseModel):
    """Output of the Mission Compiler."""

    interpretation: str = Field(description="One-paragraph restatement of the objective as understood")
    mission_kind: str = Field(default="general", description="research|implementation|investigation|decision|repair|general")
    success_criteria: list[CriterionSpec] = Field(default_factory=list)
    explicit_constraints: list[str] = Field(default_factory=list)
    inferred_constraints: list[str] = Field(default_factory=list)
    known_facts: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    unknowns: list[UnknownSpec] = Field(default_factory=list)
    hypotheses: list[HypothesisSpec] = Field(default_factory=list)
    goals: list[GoalSpec] = Field(default_factory=list)
    tasks: list[TaskSpec] = Field(default_factory=list)
    risks: list[RiskSpec] = Field(default_factory=list)
    required_artifacts: list[str] = Field(default_factory=list)
    required_tests: list[str] = Field(default_factory=list)
    human_requests: list[HumanRequestSpec] = Field(
        default_factory=list, description="Only genuinely non-inferable external decisions"
    )
    confidence_in_interpretation: float = 0.7


class SpecialistSpec(BaseModel):
    role: str = Field(description="e.g. researcher, skeptic, verifier, statistician, security_reviewer")
    objective: str
    constraints: list[str] = Field(default_factory=list)
    evidence_standard: str = Field(default="primary sources preferred; cite provenance for every claim")
    termination_criterion: str = Field(default="objective satisfied or blocked")
    context_keys: list[str] = Field(default_factory=list, description="Which state slices to include (claims, evidence, tasks, files)")
    tools: list[str] = Field(default_factory=list, description="Tool names the specialist may use; empty means reasoning only")
    independent: bool = Field(default=False, description="If True, executive conclusions are withheld to obtain independent reasoning")
    max_turns: int = 12


class ToolCallSpec(BaseModel):
    tool: str
    arguments_json: str = Field(default="{}", description="JSON object of arguments for the tool")
    purpose: str = ""

    def arguments(self) -> dict[str, Any]:
        return parse_json_object(self.arguments_json)


class StepDecision(BaseModel):
    """What the executive chooses to do next."""

    operation: OperationKind
    task_id: str = Field(default="", description="Task this step advances, if any")
    rationale: str
    expected_outcome: str = ""
    confidence: float = 0.6
    alternatives_considered: list[str] = Field(default_factory=list)
    tool_calls: list[ToolCallSpec] = Field(default_factory=list, description="For tool-backed operations")
    specialists: list[SpecialistSpec] = Field(default_factory=list, description="For instantiate_specialist / parallel_workstreams")
    reasoning_output: str = Field(default="", description="For direct_reasoning: the conclusion reached")
    human_request: Optional[HumanRequestSpec] = None
    calculation: str = Field(default="", description="For calculate: a Python expression or small program computing the value")
    simulation_json: str = Field(default="{}", description="For simulate: JSON scenario definition (see cogos.simulation)")
    wait_for_event_kind: str = ""
    consequential: bool = Field(default=False, description="Mark decisions that materially affect the outcome")


class EvidenceSpec(BaseModel):
    summary: str
    source: str
    kind: str = Field(default="secondary", description="primary|secondary|tertiary")
    supports_proposition: str = ""
    supports_claims: list[str] = Field(default_factory=list, description="Claim ids or propositions this supports")
    contradicts_claims: list[str] = Field(default_factory=list)
    scope: str = ""
    freshness: str = ""
    reliability: float = 0.5
    lineage: list[str] = Field(default_factory=list, description="Original/root sources if this is a repeat")
    excerpt: str = ""


class ClaimSpec(BaseModel):
    proposition: str
    epistemic_status: EpistemicStatus = EpistemicStatus.HYPOTHESIS
    confidence: float = 0.5
    decision_relevance: float = 0.5
    falsification_conditions: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class ClaimUpdateSpec(BaseModel):
    claim_id: str
    new_confidence: Optional[float] = None
    new_status: Optional[str] = None
    note: str = ""


class ContradictionSpec(BaseModel):
    claim_ids: list[str]
    description: str
    severity: float = 0.5
    suspected_cause: str = "unknown"


class TaskUpdateSpec(BaseModel):
    task_id: str
    status: str = Field(description="done|failed|blocked|active|pending|cancelled")
    result_summary: str = ""
    failure_reason: str = ""
    failure_kind: str = Field(default="", description="transient|structural|assumption|tool|evidence|implementation|interpretation")


class WorldUpdateSpec(BaseModel):
    entity: str
    kind: str = "thing"
    property_name: str = ""
    property_value: str = ""
    relation_to: str = ""
    relation_kind: str = ""
    epistemic_status: EpistemicStatus = EpistemicStatus.OBSERVATION
    confidence: float = 0.7


class CausalUpdateSpec(BaseModel):
    cause: str
    effect: str
    mechanism: str = ""
    strength: float = 0.5
    epistemic_status: EpistemicStatus = EpistemicStatus.HYPOTHESIS


class HypothesisUpdateSpec(BaseModel):
    hypothesis_id: str
    confidence: float = 0.5
    status: str = Field(default="active", description="active|leading|eliminated|confirmed")
    note: str = ""


class ObservationInterpretation(BaseModel):
    """How the executive integrates a result into state."""

    summary: str
    new_evidence: list[EvidenceSpec] = Field(default_factory=list)
    new_claims: list[ClaimSpec] = Field(default_factory=list)
    claim_updates: list[ClaimUpdateSpec] = Field(default_factory=list)
    contradictions: list[ContradictionSpec] = Field(default_factory=list)
    task_updates: list[TaskUpdateSpec] = Field(default_factory=list)
    new_tasks: list[TaskSpec] = Field(default_factory=list)
    resolved_unknowns: list[str] = Field(default_factory=list, description="Unknown ids resolved by this observation")
    new_unknowns: list[UnknownSpec] = Field(default_factory=list)
    world_updates: list[WorldUpdateSpec] = Field(default_factory=list)
    causal_updates: list[CausalUpdateSpec] = Field(default_factory=list)
    lessons: list[str] = Field(default_factory=list)
    failure_lessons: list[str] = Field(default_factory=list)
    hypothesis_updates: list[HypothesisUpdateSpec] = Field(default_factory=list)
    criteria_satisfied: list[str] = Field(default_factory=list, description="Success criterion ids now satisfied (only with verification)")
    progress_estimate: float = 0.0
    confidence: float = 0.5
    injection_detected: bool = False


class SpecialistFinding(BaseModel):
    statement: str
    confidence: float = 0.5
    evidence: list[EvidenceSpec] = Field(default_factory=list)
    epistemic_status: EpistemicStatus = EpistemicStatus.INFERENCE


class SpecialistReport(BaseModel):
    role: str
    conclusion: str
    confidence: float = 0.5
    findings: list[SpecialistFinding] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list, description="Paths or identifiers of produced artifacts")
    unresolved: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    blocked: bool = False
    blocked_reason: str = ""
    raw_notes: str = ""


class Disagreement(BaseModel):
    topic: str
    executive_position: str
    specialist_position: str
    material: bool = Field(default=True, description="Would resolving it change the decision?")
    resolution_plan: str = ""


class VerificationJudgment(BaseModel):
    status: str = Field(description="passed|failed|inconclusive")
    summary: str
    issues: list[str] = Field(default_factory=list)
    confidence: float = 0.6
    checked: list[str] = Field(default_factory=list, description="Which properties were actually checked")


class CriterionAssessment(BaseModel):
    criterion_id: str
    satisfied: bool
    evidence: str = ""


class Synthesis(BaseModel):
    conclusion: str
    decision: str = ""
    rationale: str
    criteria_assessment: list[CriterionAssessment] = Field(default_factory=list)
    remaining_uncertainties: list[str] = Field(default_factory=list)
    what_would_change_the_conclusion: list[str] = Field(default_factory=list)
    confidence: float = 0.5
    mission_status: str = Field(default="complete", description="complete|blocked_external|failed|active")
    blocked_by: str = ""


class Replan(BaseModel):
    """Executive response when the plan is exhausted but the mission is not complete."""

    rationale: str
    new_tasks: list[TaskSpec] = Field(default_factory=list)
    give_up: bool = Field(default=False, description="True only when no legitimate path remains")
    blocked_by: str = Field(default="", description="External dependency preventing completion, if any")
    what_would_unblock: str = ""
