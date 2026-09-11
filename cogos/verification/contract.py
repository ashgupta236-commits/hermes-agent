"""The approved acceptance contract: what the requirements demand, decided before the answer exists.

Derived from the bytes of ``REQUIREMENTS.md`` and digest-bound, so a receipt can name exactly which
expectations it was judged against. The executive may propose a contract through normal mission
compilation; it cannot approve weakened expectations, drop a deliverable, choose the authority floor
or alter isolation policy — those are controller decisions and this module is controller-owned.

Each requirement maps to **one** predicate with a stated scope, and the scope is never widened:

| requirement | predicate | what it proves |
| --- | --- | --- |
| `calc.py` exists | file present in the hashed snapshot | existence |
| `add_percent(value, percent)` returns `value` raised by `percent`%, 2dp | behavioural cases compared on the trusted side | the observed behaviour on the cases tested |
| `test_calc.py` exists | file present in the hashed snapshot | existence |
| tests cover positive, zero and negative percent | trusted AST structure of that file | that the file has that structure |

Nothing here establishes universal correctness. Finite cases establish conformance on the cases
tested; an implementation that special-cases exactly those inputs passes, which is why some cases
are drawn at run time from a controller-held seed the subject never sees. That raises the cost of
that attack. It does not close it, and this module does not claim otherwise.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

#: Bumping this changes what "verified" means, so it travels into every receipt.
CONTRACT_VERSION = "cogos.acceptance.v1"

#: Tolerance for comparing a 2-decimal rounded result. Tight enough that a wrong implementation
#: cannot hide in it, loose enough that IEEE-754 representation of a 2dp value is not a failure.
COMPARISON_TOLERANCE = 1e-9


def _expected_add_percent(value: float, percent: float) -> float:
    """The trusted reading of the requirement: *value increased by percent percent, 2dp*."""
    return round(value * (1.0 + percent / 100.0), 2)


def _is_rounding_tie(value: float, percent: float) -> bool:
    """Whether this case lands on a .xx5 boundary.

    The requirement says "rounded to 2 decimals" and does not say how ties break. Python's `round`
    is half-to-even; a `Decimal(ROUND_HALF_UP)` implementation is an equally faithful reading. A
    contract that failed one of them would be testing an expectation the requirement never states,
    so tie cases are excluded and the ambiguity is recorded rather than silently decided.
    """
    scaled = abs(value * (1.0 + percent / 100.0)) * 100.0
    return abs(scaled - int(scaled) - 0.5) < 1e-7


@dataclass(frozen=True)
class BehaviouralCase:
    case_id: str
    op: str
    args: dict[str, float]
    expected: float
    origin: str  # "requirement" | "boundary" | "derived"
    rationale: str


@dataclass(frozen=True)
class StructuralCheck:
    check_id: str
    path: str
    kind: str  # "min_test_functions" | "percent_sign_coverage"
    detail: dict[str, Any]
    rationale: str


@dataclass(frozen=True)
class SuiteCheck:
    """Run the deliverable's own suite inside the boundary, and require it to discriminate.

    A green suite is subject-reported: the exit code is chosen by the process the tests run in. What
    the controller can decide is whether that green is *about* the implementation — so the suite is
    run twice inside the boundary, once against the verified snapshot and once against a snapshot
    whose `calc.py` the controller replaced with a plausible wrong implementation. A suite that
    passes both is not testing the thing it claims to test.

    Scope: "the declared suite exits zero against the deliverable and non-zero against a
    controller-substituted incorrect implementation". Not "the tests are correct".
    """

    check_id: str
    path: str
    mutant_source: str
    rationale: str


@dataclass(frozen=True)
class AcceptanceContract:
    contract_version: str
    requirements_digest: str
    deliverables: tuple[str, ...]
    cases: tuple[BehaviouralCase, ...]
    structural: tuple[StructuralCheck, ...]
    suite: tuple[SuiteCheck, ...]
    tolerance: float
    seed: int
    #: Stated on the contract itself so a reader cannot mistake conformance for correctness.
    limitations: tuple[str, ...]

    def digest(self) -> str:
        payload = json.dumps(
            {
                "contract_version": self.contract_version,
                "requirements_digest": self.requirements_digest,
                "deliverables": list(self.deliverables),
                "cases": [asdict(c) for c in self.cases],
                "structural": [asdict(s) for s in self.structural],
                "suite": [asdict(s) for s in self.suite],
                "tolerance": self.tolerance,
                "seed": self.seed,
                # Included deliberately. The limitations are the part of the contract that says
                # conformance is not correctness; leaving them out of the digest meant a contract
                # claiming "establishes universal correctness" hashed identically to this one.
                "limitations": list(self.limitations),
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def matches(self, observed: float, case: BehaviouralCase) -> bool:
        return abs(observed - case.expected) <= self.tolerance


def derive_contract(requirements_text: str, *, seed: int = 0, derived_cases: int = 12) -> AcceptanceContract:
    """Build the approved contract from the requirement bytes.

    Deterministic in `(requirements_text, seed)`, so the same contract can be rebuilt and its digest
    re-checked later. The fixed cases come from the requirement's own wording — a positive, a zero
    and a negative percent. The derived cases are drawn from `seed`, which the controller holds and
    the subject never receives.
    """
    digest = hashlib.sha256(requirements_text.encode("utf-8")).hexdigest()

    fixed = [
        (100.0, 10.0, "requirement", "a positive percent, the requirement's central example"),
        (50.0, 0.0, "requirement", "a zero percent must return the value unchanged"),
        (200.0, -50.0, "requirement", "a negative percent must decrease the value"),
        (0.0, 25.0, "boundary", "zero value"),
        (100.0, -100.0, "boundary", "-100% reduces to zero"),
        (1.0, 0.5, "boundary", "sub-unit percent exercises the 2dp rounding"),
        (-40.0, 10.0, "boundary", "negative value with a positive percent"),
    ]
    cases: list[BehaviouralCase] = []
    for index, (value, percent, origin, rationale) in enumerate(fixed):
        if _is_rounding_tie(value, percent):
            continue
        cases.append(
            BehaviouralCase(
                case_id=f"fixed-{index}",
                op="add_percent",
                args={"value": value, "percent": percent},
                expected=_expected_add_percent(value, percent),
                origin=origin,
                rationale=rationale,
            )
        )

    rng = random.Random(seed)
    drawn = 0
    attempts = 0
    while drawn < derived_cases and attempts < derived_cases * 20:
        attempts += 1
        value = round(rng.uniform(-10_000.0, 10_000.0), 4)
        percent = round(rng.uniform(-100.0, 400.0), 4)
        if _is_rounding_tie(value, percent):
            continue
        cases.append(
            BehaviouralCase(
                case_id=f"derived-{drawn}",
                op="add_percent",
                args={"value": value, "percent": percent},
                expected=_expected_add_percent(value, percent),
                origin="derived",
                rationale="drawn from the controller-held seed; the subject does not see these inputs in advance",
            )
        )
        drawn += 1

    structural = (
        StructuralCheck(
            check_id="tests-count",
            path="test_calc.py",
            kind="min_test_functions",
            detail={"minimum": 3},
            rationale="the requirement asks for tests covering three cases",
        ),
        StructuralCheck(
            check_id="tests-percent-signs",
            path="test_calc.py",
            kind="percent_sign_coverage",
            # `conftest.py` is where pytest fixtures live by definition, and a suite that keeps its
            # cases in a fixture is ordinary rather than evasive. Reading only the test module
            # rejected exactly that arrangement. This stays a *structural* predicate either way: it
            # says percent literals of each sign appear in the suite's own sources, never that the
            # assertions are right — that is what the suite differential is for.
            detail={"required": ["positive", "zero", "negative"], "also_read": ["conftest.py"]},
            rationale="the requirement names a positive, a zero and a negative percent",
        ),
    )

    suite = (
        SuiteCheck(
            check_id="suite-discriminates",
            path="test_calc.py",
            # Plausible and wrong: it adds the percent instead of applying it, so any suite that
            # really exercises a positive percent rejects it, and one that asserts nothing does not.
            mutant_source=(
                "def add_percent(value: float, percent: float) -> float:\n"
                "    return round(value + percent, 2)\n"
            ),
            rationale="the requirement asks for tests; a suite that cannot tell a wrong implementation from a right one is not evidence that it does",
        ),
    )

    return AcceptanceContract(
        contract_version=CONTRACT_VERSION,
        requirements_digest=digest,
        deliverables=("calc.py", "test_calc.py"),
        cases=tuple(cases),
        structural=structural,
        suite=suite,
        tolerance=COMPARISON_TOLERANCE,
        seed=seed,
        limitations=(
            "Conformance on the cases tested. Not universal correctness, and not proof against an "
            "implementation that special-cases exactly these inputs.",
            "Tie-breaking at a .xx5 boundary is unspecified by the requirement, so tie cases are "
            "excluded rather than decided here.",
            "The structural checks establish the shape of test_calc.py, never that its assertions "
            "are correct.",
            "The suite check establishes that the declared suite discriminates one controller-chosen "
            "wrong implementation. It is not a claim that the suite is complete.",
        ),
    )


# -- binding the contract to a mission ----------------------------------------------------

#: Where the approved contract lives on the mission. Written by the controller when the mission is
#: compiled; the executive can propose a compilation but cannot write this key, because nothing in
#: the executive's action space reaches `state.resources` directly.
ACCEPTANCE_KEY = "acceptance"

#: The predicate families a criterion can be mapped to, and what each is allowed to prove.
PREDICATE_SCOPES = {
    "behaviour": "observed responses to controller-chosen inputs matched controller-held expectations",
    "structure": "the deliverable's source has the declared shape",
    "suite_differential": "the declared suite passes against the deliverable and fails against a controller-substituted wrong implementation",
    "existence": "the declared file is present in the hashed snapshot",
}


def map_criteria(criteria: "list", contract: AcceptanceContract) -> dict[str, list[str]]:
    """Assign each criterion the predicates that may close it. Controller-decided, deterministic.

    Token overlap is not used. A criterion is read only for which *facet of the requirement* it
    names — the feature itself, or the tests the requirement also asks for — and is then bound to
    the predicate family that can actually decide that facet. A criterion naming neither gets no
    predicates, which means no behavioural receipt can close it and it must be satisfied some other
    way or block; silently widening it to "something passed" is the failure mode this replaces.
    """
    mapping: dict[str, list[str]] = {}
    for criterion in criteria:
        text = (f"{criterion.description} {criterion.verification_method}").lower()
        names_tests = any(word in text for word in ("test suite", "tests pass", "suite passes", "pytest", "test coverage"))
        names_feature = any(word in text for word in ("feature", "implement", "implemented", "requirement", "function", "behaviour", "behavior", "correct"))
        predicates: list[str] = []
        if names_feature:
            predicates = ["existence", "behaviour", "structure", "suite_differential"]
        elif names_tests:
            predicates = ["existence", "structure", "suite_differential"]
        if predicates:
            mapping[criterion.id] = predicates
    return mapping


def approve_acceptance(state, contract: AcceptanceContract) -> dict:
    """Record the approved contract and criterion mapping on the mission, and return it.

    Called by the controller. The digest is what a receipt cites, so a contract swapped after the
    fact does not silently re-interpret evidence already recorded against the old one.
    """
    record = {
        "contract_version": contract.contract_version,
        "contract_digest": contract.digest(),
        "requirements_digest": contract.requirements_digest,
        "deliverables": list(contract.deliverables),
        "criteria": map_criteria(state.success_criteria, contract),
        "scopes": dict(PREDICATE_SCOPES),
    }
    state.resources[ACCEPTANCE_KEY] = record
    return record
