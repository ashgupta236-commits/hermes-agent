"""Failure diagnosis and matched intervention (L4).

The useful question after a failure is not "should we try harder?" but "what kind of failure was
that?". A missing primary source and an insufficient reasoning allocation look identical from
the outside and have nothing in common as problems, so this module classifies a failure into one
of ten named classes and picks the intervention that actually addresses *that* class.

Two boundaries are structural:

* **Authorization and safety refusal are not capability failures.** They get their own classes,
  and their interventions are "continue an authorized alternative" and "respect the denial" —
  never rewording, rerouting to another agent, or any other search for a way past. There is no
  branch in this module that returns a bypass, and a test asserts it.
* **Repeating an attempt is not an intervention.** ``REPEATED_INEFFECTIVE`` exists precisely so
  that "try it again" is diagnosed as the non-answer it is, and no improvement is credited for
  more calls or a restatement of the same attempt.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from cogos.ids import iso_now, new_id


class FailureClass(str, Enum):
    INCORRECT_PREMISE = "incorrect_premise"
    MISSING_EVIDENCE = "missing_evidence"
    DECOMPOSITION = "decomposition"
    REPRESENTATION = "representation"
    TOOL_ENVIRONMENT = "tool_environment"
    REASONING_ALLOCATION = "reasoning_allocation"
    VERIFIER_WEAKNESS = "verifier_weakness"
    REPEATED_INEFFECTIVE = "repeated_ineffective"
    UNRESOLVED_AMBIGUITY = "unresolved_ambiguity"
    AUTHORIZATION_REFUSAL = "authorization_refusal"


#: Classes where the correct response is *not* to try harder at the same thing.
NOT_A_CAPABILITY_PROBLEM = frozenset({FailureClass.AUTHORIZATION_REFUSAL})


class Intervention(BaseModel):
    id: str = Field(default_factory=lambda: new_id("int"))
    failure_class: FailureClass
    action: str
    rationale: str
    tests_the_diagnosis: str = Field(description="What would be observed if the diagnosis were right")
    increases_authority: bool = Field(default=False, description="Always False: an intervention never widens what the runtime may do")
    created_at: str = Field(default_factory=iso_now)


class Diagnosis(BaseModel):
    failure_class: FailureClass
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    signals: list[str] = Field(default_factory=list)
    intervention: Intervention


_PATTERNS: list[tuple[FailureClass, re.Pattern[str], str]] = [
    (FailureClass.AUTHORIZATION_REFUSAL, re.compile(r"\b(denied|not authori[sz]ed|requires? (human|explicit) authori[sz]ation|permission|refus(al|ed)|policy)\b", re.I), "the operation was refused or requires authorization"),
    (FailureClass.TOOL_ENVIRONMENT, re.compile(r"\b(not found on path|unavailable|no such (file|tool|command)|command not found|connection (refused|reset)|missing (tool|binary|dependency))\b", re.I), "the environment could not provide the capability"),
    (FailureClass.MISSING_EVIDENCE, re.compile(r"\b(no (claim|evidence|source|observation)|insufficient evidence|unsourced|could not establish|no observation bears)\b", re.I), "the conclusion had nothing behind it"),
    (FailureClass.VERIFIER_WEAKNESS, re.compile(r"\b(not machine-checkable|inconclusive|undecidable|no verification method|cannot be decided)\b", re.I), "the check could not decide either way"),
    (FailureClass.UNRESOLVED_AMBIGUITY, re.compile(r"\b(ambiguous|unclear (requirement|spec)|multiple interpretations|underspecified)\b", re.I), "the requirement admits more than one reading"),
    (FailureClass.INCORRECT_PREMISE, re.compile(r"\b(false premise|premise (is )?(wrong|incorrect)|assumption (was )?(wrong|invalid)|contradict(s|ed) the evidence|refut(ed|es))\b", re.I), "the starting assumption did not hold"),
    (FailureClass.REPRESENTATION, re.compile(r"\b(wrong (units?|format|encoding|type)|schema mismatch|parse (error|failure)|malformed|type ?error)\b", re.I), "the data was in a form the step could not use"),
    (FailureClass.DECOMPOSITION, re.compile(r"\b(too (large|broad)|scope|prerequisite|dependenc(y|ies) (missing|unmet)|step (was )?too big)\b", re.I), "the step bundled work that needed splitting"),
    (FailureClass.REASONING_ALLOCATION, re.compile(r"\b(truncated|max[_ ]turns|ran out of (turns|steps)|token limit|incomplete reasoning)\b", re.I), "the step was cut off before it finished"),
]


def diagnose(
    failure_text: str,
    *,
    attempts: int = 1,
    previous_signatures: Optional[list[str]] = None,
    signature: str = "",
) -> Diagnosis:
    """Classify a failure, then choose the intervention that addresses that class.

    A structurally identical repeat outranks the textual classification: whatever the error says,
    the third occurrence of the same signature means the intervention chosen last time was the
    wrong kind, and repeating it a fourth time is not a plan.
    """
    signals: list[str] = []
    prior = list(previous_signatures or [])
    if signature and prior.count(signature) >= 2:
        signals.append(f"failure signature '{signature[:40]}' has now occurred {prior.count(signature) + 1} times")
        return Diagnosis(failure_class=FailureClass.REPEATED_INEFFECTIVE, confidence=0.9, signals=signals, intervention=intervention_for(FailureClass.REPEATED_INEFFECTIVE))

    text = failure_text or ""
    for klass, pattern, why in _PATTERNS:
        if pattern.search(text):
            signals.append(why)
            confidence = 0.8 if klass in (FailureClass.AUTHORIZATION_REFUSAL, FailureClass.TOOL_ENVIRONMENT) else 0.65
            return Diagnosis(failure_class=klass, confidence=confidence, signals=signals, intervention=intervention_for(klass))

    if attempts >= 3:
        signals.append(f"{attempts} attempts with no classified cause")
        return Diagnosis(failure_class=FailureClass.REPEATED_INEFFECTIVE, confidence=0.6, signals=signals, intervention=intervention_for(FailureClass.REPEATED_INEFFECTIVE))
    signals.append("no diagnostic signal matched; treating it as an evidence gap rather than guessing")
    return Diagnosis(failure_class=FailureClass.MISSING_EVIDENCE, confidence=0.35, signals=signals, intervention=intervention_for(FailureClass.MISSING_EVIDENCE))


_INTERVENTIONS: dict[FailureClass, tuple[str, str, str]] = {
    FailureClass.INCORRECT_PREMISE: (
        "state the premise explicitly as a claim and run a discriminating check against it",
        "if the starting assumption is wrong, more effort on top of it produces a better-argued wrong answer",
        "the discriminating check refutes the premise, or supports it and the failure has another cause",
    ),
    FailureClass.MISSING_EVIDENCE: (
        "retrieve the missing primary source and attach it to the claim",
        "the conclusion is unsupported, which no amount of reasoning about it repairs",
        "the retrieved source either establishes the proposition or names what is still absent",
    ),
    FailureClass.DECOMPOSITION: (
        "split the step into prerequisites and sequence them",
        "the step bundled several problems, so its failure identifies none of them",
        "at least one sub-step succeeds where the whole failed, isolating the real blocker",
    ),
    FailureClass.REPRESENTATION: (
        "convert the data to the form the step requires and re-run it unchanged",
        "the reasoning may be correct on a representation the step could not read",
        "the same step succeeds on converted input, or fails identically and the cause is elsewhere",
    ),
    FailureClass.TOOL_ENVIRONMENT: (
        "use an available alternative capability, or record the missing tool as a blocked operation",
        "the environment could not do it; that is a fact about the environment, not about the plan",
        "the alternative capability produces the effect, or the blocked operation names what would unblock it",
    ),
    FailureClass.REASONING_ALLOCATION: (
        "raise the supported effort or turn allowance for this bounded step only, and compare at equal total budget",
        "the step was cut off rather than wrong; extra allocation is only justified where outcomes show it pays",
        "the same step completes within the raised allowance, at a cost the comparison can weigh",
    ),
    FailureClass.VERIFIER_WEAKNESS: (
        "make the criterion checkable: name the observation, artifact or command that would settle it",
        "an undecidable check cannot fail honestly or pass honestly",
        "the restated criterion resolves to passed or failed instead of inconclusive",
    ),
    FailureClass.REPEATED_INEFFECTIVE: (
        "stop this approach; change the class of attempt or record the branch as blocked with what is missing",
        "the same structural attempt has already failed; a further identical attempt carries no new information",
        "a different class of attempt produces a different failure, or the branch is honestly reported blocked",
    ),
    FailureClass.UNRESOLVED_AMBIGUITY: (
        "resolve the ambiguity explicitly: choose the reading, record it as an assumption, and proceed",
        "work on an unresolved reading satisfies neither interpretation",
        "the recorded assumption makes the criterion decidable one way or the other",
    ),
    FailureClass.AUTHORIZATION_REFUSAL: (
        "respect the denial and continue an authorized alternative that can still satisfy the goal, or report the limitation with evidence",
        "a refusal is a decision about the action, not a gap in capability; it is not something to route around",
        "an authorized alternative advances the goal, or the limitation is reported with what authorization would be required",
    ),
}


def intervention_for(failure_class: FailureClass) -> Intervention:
    action, rationale, test = _INTERVENTIONS[failure_class]
    return Intervention(failure_class=failure_class, action=action, rationale=rationale, tests_the_diagnosis=test, increases_authority=False)


# -- research direction selection (L4) --------------------------------------------------------


class ResearchOption(BaseModel):
    """One hypothesis in the portfolio, with the cheap experiment that could kill it."""

    id: str = Field(default_factory=lambda: new_id("ropt"))
    hypothesis: str
    current_evidence: list[str] = Field(default_factory=list)
    plausible_alternatives: list[str] = Field(default_factory=list)
    falsifying_experiment: str = ""
    expected_information: float = Field(default=0.5, ge=0.0, le=1.0, description="A prior, not a validated outcome")
    resource_estimate: float = Field(default=0.1, ge=0.0)
    stopping_condition: str = ""
    attempts: int = 0
    informative_attempts: int = 0

    def measured_usefulness(self) -> Optional[float]:
        """Observed hit rate, or None when nothing has been tried yet.

        Kept separate from `expected_information` on purpose: a model's estimate of novelty or
        importance is a prior, and calling it a result is how a portfolio talks itself into a
        dead branch.
        """
        if self.attempts == 0:
            return None
        return self.informative_attempts / self.attempts

    def score(self) -> float:
        """Expected information per unit resource, shrunk toward measured history."""
        prior = self.expected_information
        measured = self.measured_usefulness()
        blended = prior if measured is None else (0.4 * prior + 0.6 * measured)
        if not self.falsifying_experiment:
            blended *= 0.5  # a hypothesis with no way to be wrong is worth less, not more
        return round(blended / max(0.01, self.resource_estimate), 6)

    def exhausted(self, max_uninformative: int = 3) -> bool:
        return self.attempts - self.informative_attempts >= max_uninformative and (self.measured_usefulness() or 0.0) < 0.34


class ResearchSelector:
    """Ranks a portfolio by measured usefulness under uncertainty, and stops dead branches."""

    def __init__(self, options: Optional[list[ResearchOption]] = None, max_uninformative: int = 3):
        self.options = list(options or [])
        self.max_uninformative = max_uninformative

    def add(self, option: ResearchOption) -> ResearchOption:
        self.options.append(option)
        return option

    def active(self) -> list[ResearchOption]:
        return [o for o in self.options if not o.exhausted(self.max_uninformative)]

    def next_option(self) -> Optional[ResearchOption]:
        candidates = self.active()
        if not candidates:
            return None
        return max(candidates, key=lambda o: o.score())

    def record_result(self, option: ResearchOption, *, informative: bool, disconfirmed: bool = False) -> dict[str, Any]:
        """Update the portfolio from what the experiment actually showed."""
        option.attempts += 1
        if informative:
            option.informative_attempts += 1
        note = ""
        if disconfirmed:
            # Disconfirmation is a result, not a setback: the hypothesis is revised and its
            # alternatives are promoted rather than the branch being re-run unchanged.
            note = f"hypothesis disconfirmed; promoting {len(option.plausible_alternatives)} alternative(s)"
            for alt in option.plausible_alternatives:
                self.add(
                    ResearchOption(
                        hypothesis=alt,
                        current_evidence=[f"promoted after '{option.hypothesis[:80]}' was disconfirmed"],
                        falsifying_experiment=option.falsifying_experiment,
                        expected_information=min(1.0, option.expected_information + 0.1),
                        resource_estimate=option.resource_estimate,
                        stopping_condition=option.stopping_condition,
                    )
                )
            option.expected_information = max(0.0, option.expected_information - 0.4)
        return {
            "option_id": option.id,
            "attempts": option.attempts,
            "measured_usefulness": option.measured_usefulness(),
            "exhausted": option.exhausted(self.max_uninformative),
            "note": note,
        }
