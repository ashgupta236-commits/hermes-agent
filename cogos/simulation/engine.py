"""Deterministic counterfactual world engine.

Given a decision question and a set of options, each with a discrete outcome
distribution, the engine computes exact expected values analytically, runs a
seeded Monte Carlo for the shape of the distribution (percentiles and regret),
and stress-tests the ranking against every assumption extreme.

Value model for a single outcome of an option::

    value = benefit * multiplier(option assumptions) - cost - risk

where ``multiplier`` is the product of the option's assumption multipliers.
Assumption ranges on the scenario define how far each multiplier may swing;
sensitivity, scenario analysis and EVPI are all computed by re-evaluating the
analytic expected values with one assumption pinned to an extreme.

Everything is deterministic for a given ``seed``: the Monte Carlo uses a
private ``random.Random`` instance and iteration order is the declaration
order of options and outcomes.
"""

from __future__ import annotations

import math
import random
from typing import Any, Union

from pydantic import BaseModel, Field

PROBABILITY_TOLERANCE = 0.05


class Outcome(BaseModel):
    name: str
    probability: float = Field(ge=0.0, description="Weight of this outcome; normalised across the option")
    benefit: float = 0.0
    cost: float = 0.0
    risk: float = 0.0
    description: str = ""


class Option(BaseModel):
    name: str
    description: str = ""
    outcomes: list[Outcome] = Field(default_factory=list)
    reversibility: str = Field(default="reversible", description="reversible|partially_reversible|irreversible")
    second_order_effects: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    assumptions: dict[str, float] = Field(
        default_factory=dict, description="Assumption name -> multiplier applied to this option's benefits (default 1.0)"
    )
    information_missing: list[str] = Field(default_factory=list)


class Scenario(BaseModel):
    question: str
    options: list[Option] = Field(default_factory=list)
    seed: int = 0
    samples: int = Field(default=2000, ge=1)
    assumption_ranges: dict[str, tuple[float, float]] = Field(
        default_factory=dict, description="Assumption name -> (low, high) range of its multiplier"
    )


class OptionResult(BaseModel):
    name: str
    expected_value: float
    expected_benefit: float
    expected_cost: float
    expected_risk: float
    p10: float
    p50: float
    p90: float
    regret: float = Field(description="Mean per-sample shortfall versus the best option in that sample")
    reversibility: str
    dominant: bool = Field(description="Pareto-dominates every other option on benefit, cost and risk")
    sensitivity: dict[str, float] = Field(default_factory=dict, description="EV(high) - EV(low) per assumption")
    information_missing: list[str] = Field(default_factory=list)


class SimulationResult(BaseModel):
    question: str
    results: list[OptionResult] = Field(default_factory=list)
    best_option: str = ""
    margin: float = Field(default=0.0, description="EV gap between the best and second-best option")
    robust_best: bool = Field(default=False, description="Best option stays best at every assumption extreme")
    warnings: list[str] = Field(default_factory=list)
    scenario_analysis: list[dict[str, Any]] = Field(default_factory=list)
    method: str = ""


ScenarioLike = Union[Scenario, dict[str, Any]]


# --- helpers ---------------------------------------------------------------------


def _coerce(scenario: ScenarioLike) -> Scenario:
    if isinstance(scenario, Scenario):
        return scenario
    return Scenario.model_validate(scenario)


def _normalised_probabilities(option: Option, warnings: list[str]) -> list[float]:
    """Normalise an option's outcome probabilities, warning when they are far from 1."""
    probs = [max(0.0, float(o.probability)) for o in option.outcomes]
    if not probs:
        warnings.append(f"option '{option.name}' has no outcomes; treated as zero-value")
        return []
    total = sum(probs)
    if total <= 0.0:
        warnings.append(f"option '{option.name}' probabilities sum to 0; using a uniform distribution")
        return [1.0 / len(probs)] * len(probs)
    if abs(total - 1.0) > PROBABILITY_TOLERANCE:
        warnings.append(f"option '{option.name}' probabilities sum to {total:.3f}, not 1.0; normalised")
    return [p / total for p in probs]


def _multiplier(option: Option, overrides: dict[str, float]) -> float:
    """Product of the option's assumption multipliers with selected overrides applied."""
    mult = 1.0
    for name, value in option.assumptions.items():
        mult *= overrides.get(name, value)
    return mult


def _analytic(option: Option, probs: list[float], overrides: dict[str, float]) -> tuple[float, float, float, float]:
    """Return (EV, expected benefit, expected cost, expected risk) for an option."""
    mult = _multiplier(option, overrides)
    ev = benefit = cost = risk = 0.0
    for p, outcome in zip(probs, option.outcomes):
        b = outcome.benefit * mult
        benefit += p * b
        cost += p * outcome.cost
        risk += p * outcome.risk
        ev += p * (b - outcome.cost - outcome.risk)
    return ev, benefit, cost, risk


def _evs(scenario: Scenario, probs: list[list[float]], overrides: dict[str, float]) -> list[float]:
    return [_analytic(opt, pr, overrides)[0] for opt, pr in zip(scenario.options, probs)]


def _argmax(values: list[float]) -> int:
    """Index of the maximum value; ties resolve to the earliest declared option."""
    best = 0
    for i, v in enumerate(values):
        if v > values[best]:
            best = i
    return best


def _percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def _draw_outcome(rng: random.Random, probs: list[float]) -> int:
    u = rng.random()
    acc = 0.0
    for i, p in enumerate(probs):
        acc += p
        if u < acc:
            return i
    return len(probs) - 1


def _is_pareto_dominant(index: int, stats: list[tuple[float, float, float, float]]) -> bool:
    """Option ``index`` dominates if it is at least as good as every other option on
    benefit, cost and risk and strictly better on at least one dimension for each."""
    if len(stats) < 2:
        return False
    _, b, c, r = stats[index]
    for j, (_, ob, oc, orisk) in enumerate(stats):
        if j == index:
            continue
        if not (b >= ob and c <= oc and r <= orisk):
            return False
        if not (b > ob or c < oc or r < orisk):
            return False
    return True


# --- public API ------------------------------------------------------------------


def scenario_schema() -> dict[str, Any]:
    """JSON schema for :class:`Scenario`, suitable for prompting a model to emit one."""
    return Scenario.model_json_schema()


def simulate(scenario: ScenarioLike) -> SimulationResult:
    """Evaluate every option of a scenario analytically and by seeded Monte Carlo."""
    sc = _coerce(scenario)
    warnings: list[str] = []
    method = (
        "analytic expected values over discrete outcome distributions; "
        f"seeded Monte Carlo (seed={sc.seed}, samples={sc.samples}) with common random numbers across options "
        "for percentiles and regret (assumptions with ranges are drawn uniformly per sample); "
        "sensitivity and scenario analysis re-evaluate analytic EV with one assumption pinned to each extreme"
    )
    if not sc.options:
        warnings.append("scenario has no options")
        return SimulationResult(question=sc.question, warnings=warnings, method=method)

    probs = [_normalised_probabilities(opt, warnings) for opt in sc.options]
    ranges = {k: (min(v), max(v)) for k, v in sc.assumption_ranges.items()}
    for opt in sc.options:
        for name in opt.assumptions:
            if name not in ranges:
                warnings.append(f"assumption '{name}' on option '{opt.name}' has no range; not stress-tested")

    # --- analytic ----------------------------------------------------------------
    stats = [_analytic(opt, pr, {}) for opt, pr in zip(sc.options, probs)]
    evs = [s[0] for s in stats]
    best_index = _argmax(evs)
    best_name = sc.options[best_index].name
    others = [ev for i, ev in enumerate(evs) if i != best_index]
    margin = evs[best_index] - max(others) if others else 0.0

    # --- sensitivity & scenario analysis -----------------------------------------
    sensitivity: list[dict[str, float]] = [{} for _ in sc.options]
    scenario_analysis: list[dict[str, Any]] = []
    robust = True
    for name in sorted(ranges):
        low, high = ranges[name]
        ev_low = _evs(sc, probs, {name: low})
        ev_high = _evs(sc, probs, {name: high})
        for i in range(len(sc.options)):
            sensitivity[i][name] = ev_high[i] - ev_low[i]
        for label, value, ev_case in (("low", low, ev_low), ("high", high, ev_high)):
            winner_index = _argmax(ev_case)
            winner = sc.options[winner_index].name
            if winner != best_name:
                robust = False
            scenario_analysis.append(
                {
                    "assumption": name,
                    "extreme": label,
                    "value": value,
                    "winner": winner,
                    "expected_values": {opt.name: ev for opt, ev in zip(sc.options, ev_case)},
                }
            )

    # --- Monte Carlo ---------------------------------------------------------------
    rng = random.Random(sc.seed)
    n_opts = len(sc.options)
    samples: list[list[float]] = [[] for _ in range(n_opts)]
    regret_sums = [0.0] * n_opts
    range_names = sorted(ranges)
    for _ in range(sc.samples):
        overrides = {name: rng.uniform(*ranges[name]) for name in range_names}
        values: list[float] = []
        for i, opt in enumerate(sc.options):
            if not probs[i]:
                values.append(0.0)
                continue
            outcome = opt.outcomes[_draw_outcome(rng, probs[i])]
            mult = _multiplier(opt, overrides)
            values.append(outcome.benefit * mult - outcome.cost - outcome.risk)
        best_value = max(values)
        for i, v in enumerate(values):
            samples[i].append(v)
            regret_sums[i] += best_value - v

    results: list[OptionResult] = []
    for i, opt in enumerate(sc.options):
        ordered = sorted(samples[i])
        ev, benefit, cost, risk = stats[i]
        results.append(
            OptionResult(
                name=opt.name,
                expected_value=ev,
                expected_benefit=benefit,
                expected_cost=cost,
                expected_risk=risk,
                p10=_percentile(ordered, 0.10),
                p50=_percentile(ordered, 0.50),
                p90=_percentile(ordered, 0.90),
                regret=regret_sums[i] / sc.samples,
                reversibility=opt.reversibility,
                dominant=_is_pareto_dominant(i, stats),
                sensitivity=sensitivity[i],
                information_missing=list(opt.information_missing),
            )
        )

    if others and margin == 0.0:
        warnings.append(f"tie on expected value; '{best_name}' selected by declaration order")
    if not robust:
        warnings.append("ranking is sensitive to assumptions: the best option changes at some assumption extreme")
    best_opt = sc.options[best_index]
    if best_opt.reversibility == "irreversible" and others and margin < abs(evs[best_index]) * 0.1:
        warnings.append(f"best option '{best_name}' is irreversible and wins by a thin margin")

    return SimulationResult(
        question=sc.question,
        results=results,
        best_option=best_name,
        margin=margin,
        robust_best=robust,
        warnings=warnings,
        scenario_analysis=scenario_analysis,
        method=method,
    )


def expected_value_of_information(scenario: ScenarioLike, assumption: str) -> float:
    """EVPI-style value of learning an assumption's true value before choosing.

    The assumption's two extremes are treated as equiprobable states of the
    world. With perfect information the decider picks the best option in each
    state; without it they pick the option with the best average EV across the
    two states. The difference is never negative.
    """
    sc = _coerce(scenario)
    if not sc.options or assumption not in sc.assumption_ranges:
        return 0.0
    warnings: list[str] = []
    probs = [_normalised_probabilities(opt, warnings) for opt in sc.options]
    low, high = sc.assumption_ranges[assumption]
    ev_low = _evs(sc, probs, {assumption: min(low, high)})
    ev_high = _evs(sc, probs, {assumption: max(low, high)})
    with_info = 0.5 * max(ev_low) + 0.5 * max(ev_high)
    without_info = max(0.5 * a + 0.5 * b for a, b in zip(ev_low, ev_high))
    return max(0.0, with_info - without_info)
