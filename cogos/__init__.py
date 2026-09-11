"""cogos — a persistent, high-autonomy cognitive operating system around Claude.

The package is deliberately independent of the Hermes core (see AGENTS.md: the
core is a narrow waist; capability lives at the edges). It provides:

* a Mission Compiler that turns a human objective into durable structured state,
* a persistent executive loop (perceive → update → select → act → verify → learn),
* world model, belief/evidence graph, hypothesis tournament, memory classes,
* a dynamic Agent Foundry with frontier-model residency,
* a capability firewall and cognitive immune system separate from cognition,
* verification, observability, decision journal, calibration, events, skills,
* an evaluation harness with adversarial scenarios.

Entry points: ``python -m cogos --help`` and :mod:`cogos.runtime`.
"""

__version__ = "0.1.0"
