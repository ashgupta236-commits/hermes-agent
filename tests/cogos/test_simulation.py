"""Tests for the deterministic counterfactual world engine."""

from __future__ import annotations

import pytest

from cogos.simulation import (
    Option,
    Outcome,
    Scenario,
    SimulationResult,
    expected_value_of_information,
    scenario_schema,
    simulate,
)


def _scenario(**overrides) -> Scenario:
    base = dict(
        question="Ship the risky feature or the safe one?",
        options=[
            Option(
                name="risky",
                outcomes=[
                    Outcome(name="hit", probability=0.6, benefit=100, cost=20, risk=5),
                    Outcome(name="miss", probability=0.4, benefit=10, cost=20, risk=5),
                ],
                assumptions={"market_growth": 1.0},
                reversibility="irreversible",
                information_missing=["conversion rate"],
            ),
            Option(
                name="safe",
                outcomes=[Outcome(name="steady", probability=1.0, benefit=50, cost=10)],
            ),
        ],
        seed=7,
        samples=1500,
        assumption_ranges={"market_growth": (0.5, 1.5)},
    )
    base.update(overrides)
    return Scenario(**base)


def test_determinism_same_seed_identical_results():
    a = simulate(_scenario())
    b = simulate(_scenario())
    assert a.model_dump() == b.model_dump()
    c = simulate(_scenario(seed=8))
    assert [r.p50 for r in a.results] != [r.p50 for r in c.results]


def test_accepts_dict_and_analytic_expected_values():
    result = simulate(_scenario().model_dump())
    assert isinstance(result, SimulationResult)
    by_name = {r.name: r for r in result.results}
    # risky: 0.6*(100-20-5) + 0.4*(10-20-5) = 45 - 6 = 39 ; safe: 40
    assert by_name["risky"].expected_value == pytest.approx(39.0)
    assert by_name["risky"].expected_benefit == pytest.approx(64.0)
    assert by_name["risky"].expected_cost == pytest.approx(20.0)
    assert by_name["risky"].expected_risk == pytest.approx(5.0)
    assert by_name["safe"].expected_value == pytest.approx(40.0)
    assert result.best_option == "safe"
    assert result.margin == pytest.approx(1.0)
    assert by_name["safe"].p10 == by_name["safe"].p50 == by_name["safe"].p90 == pytest.approx(40.0)
    assert by_name["risky"].p10 < by_name["risky"].p50 < by_name["risky"].p90
    assert by_name["risky"].information_missing == ["conversion rate"]
    assert by_name["risky"].reversibility == "irreversible"


def test_robustness_and_scenario_analysis():
    result = simulate(_scenario())
    # at market_growth=1.5 risky wins, at 0.5 safe wins -> not robust
    assert result.robust_best is False
    winners = {(row["assumption"], row["extreme"]): row["winner"] for row in result.scenario_analysis}
    assert winners[("market_growth", "low")] == "safe"
    assert winners[("market_growth", "high")] == "risky"
    assert any("sensitive to assumptions" in w for w in result.warnings)

    robust = simulate(_scenario(assumption_ranges={"market_growth": (0.9, 1.0)}))
    assert robust.best_option == "safe"
    assert robust.robust_best is True


def test_dominance_and_margin():
    dominated = Option(name="worse", outcomes=[Outcome(name="only", probability=1.0, benefit=30, cost=20, risk=1)])
    better = Option(name="better", outcomes=[Outcome(name="only", probability=1.0, benefit=60, cost=10, risk=0)])
    result = simulate(Scenario(question="q", options=[dominated, better], samples=100))
    by_name = {r.name: r for r in result.results}
    assert by_name["better"].dominant is True
    assert by_name["worse"].dominant is False
    assert result.best_option == "better"
    assert result.margin == pytest.approx(50.0 - 9.0)
    assert result.robust_best is True
    assert by_name["better"].regret == pytest.approx(0.0)
    assert by_name["worse"].regret == pytest.approx(41.0)


def test_sensitivity_sign_follows_benefit_multiplier():
    result = simulate(_scenario())
    by_name = {r.name: r for r in result.results}
    # higher multiplier -> higher benefit -> positive EV swing for the option carrying the assumption
    assert by_name["risky"].sensitivity["market_growth"] > 0
    assert by_name["risky"].sensitivity["market_growth"] == pytest.approx(64.0)
    assert by_name["safe"].sensitivity["market_growth"] == pytest.approx(0.0)


def test_evpi_non_negative_and_positive_when_decision_flips():
    sc = _scenario()
    evpi = expected_value_of_information(sc, "market_growth")
    assert evpi >= 0
    # low state: safe 40 vs risky 7 ; high state: risky 71 vs safe 40
    assert evpi == pytest.approx(0.5 * 40 + 0.5 * 71 - max(0.5 * (7 + 71), 40))
    assert expected_value_of_information(sc, "unknown_assumption") == 0.0
    narrow = _scenario(assumption_ranges={"market_growth": (0.9, 1.0)})
    assert expected_value_of_information(narrow, "market_growth") == pytest.approx(0.0)


def test_probability_normalisation_warning():
    off = Scenario(
        question="q",
        options=[
            Option(name="a", outcomes=[Outcome(name="x", probability=0.5, benefit=10), Outcome(name="y", probability=0.2, benefit=0)]),
            Option(name="b", outcomes=[Outcome(name="x", probability=0.51, benefit=5), Outcome(name="y", probability=0.51, benefit=5)]),
        ],
        samples=50,
    )
    result = simulate(off)
    assert any("'a'" in w and "normalised" in w for w in result.warnings)
    assert not any("'b'" in w for w in result.warnings)
    by_name = {r.name: r for r in result.results}
    assert by_name["a"].expected_value == pytest.approx(10 * 0.5 / 0.7)
    assert by_name["b"].expected_value == pytest.approx(5.0)


def test_scenario_schema_is_json_schema():
    schema = scenario_schema()
    assert schema["title"] == "Scenario"
    assert "question" in schema["properties"]
    assert "options" in schema["properties"]
    assert "$defs" in schema and "Option" in schema["$defs"] and "Outcome" in schema["$defs"]
