"""Regression suite for the NOVRA Tier-0 triage plugin.

Mirrors novra-os/scripts/test-prompt-triage.sh so the two implementations cannot drift
silently. The shell hook runs on Claude Code; this plugin runs on the Hermes gateway;
both must sort the same prompt into the same lane.

The plugin directory is hyphenated, so it is loaded by path rather than imported.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

_PLUGIN = (pathlib.Path(__file__).resolve().parents[2]
           / "plugins" / "novra-triage" / "__init__.py")
_spec = importlib.util.spec_from_file_location("novra_triage", _PLUGIN)
novra_triage = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(novra_triage)

lane = lambda p, interactive=True: novra_triage.classify(p, interactive=interactive)[0]  # noqa: E731
kind = lambda p: novra_triage.classify(p)[1]  # noqa: E731


# ── GREEN: the default. Silence here is the point, not a gap. ────────────────
@pytest.mark.parametrize("prompt", [
    "add a test for the parser",
    "what does the deploy script do?",
    "yes",
    "ok go",
    "raw: just do it",
    "/wiki-query palmsip",
    "explain how the beit tokens are structured",
    "order the imports alphabetically",
    "charge ahead with the refactor",
])
def test_green(prompt):
    assert lane(prompt) == "GREEN", prompt


# ── RED: reserved acts ──────────────────────────────────────────────────────
@pytest.mark.parametrize("prompt,expect", [
    ("wire 5000 usd to the co-packer", "reserved-act (money)"),
    ("sign the co-packer contract", "reserved-act (legal bind)"),
    ("incorporate the entity in the freezone", "reserved-act (entity/banking)"),
    ("post this as ash on linkedin", "reserved-act (speak as Ash / mass publish)"),
    ("drop the production database", "reserved-act (destroy prod)"),
    ("set FIRM_LIVE=1", "reserved-act (live money)"),
    ("unfreeze the dashboard", "reserved-act (freeze/hold change — #8)"),
    ("wake-jarvis", "reserved-act (freeze/hold change — #8)"),
])
def test_red_reserved_acts(prompt, expect):
    got_lane, got_kind = novra_triage.classify(prompt)
    assert got_lane == "RED", prompt
    assert got_kind == expect, f"{prompt}: {got_kind}"


# ── RED: frozen surfaces, and the doc exemption that must stay anchored ──────
def test_frozen_surface_reds():
    assert lane("redesign the beit hero") == "RED"
    assert lane("design packaging for beit al muharraq") == "RED"
    assert lane("update the jarvis dashboard") == "RED"


def test_known_gap_edit_verb_list_is_not_exhaustive():
    """"tweak" is not in the edit-verb list — here OR in the shell hook.

    Pinned as a KNOWN GAP shared by both implementations rather than silently widened
    here, because a verb added on one side only is exactly the drift this suite exists to
    prevent. The list already grew once (design/create/make/generate were missing and
    "design packaging for Beit al Muharraq" walked straight through), so the gap class is
    real. Closing it is a change to both files in one PR, not a unilateral fix in the
    port. If this ever starts returning RED, the two sides have diverged.
    """
    assert lane("tweak the jarvis dashboard") == "GREEN"


def test_doc_exemption_is_anchored_not_merely_present():
    # A doc noun as the verb's OBJECT is exempt — canon §8 mandates a session capture
    # that will name the frozen surface.
    assert lane("write the session capture for the beit freeze") == "GREEN"
    # But a doc noun in a trailing aside must NOT disarm the wall. As a whole-prompt
    # check this was a working bypass of a T0 wall.
    assert lane("update the beit hero copy, see the wiki for context") == "RED"


# ── RED: cross-entity aggregation (#7), including the collective phrasing ────
def test_cross_entity_aggregation():
    assert lane("compare beit and franklins revenue") == "RED"
    assert lane("which venture is doing better overall") == "RED"
    # A single entity is NOT aggregation — 'beit|muharraq' is one alternation so
    # "Beit al Muharraq" scores 1, not 2. (Phrased without an edit verb on purpose:
    # "launch" IS one, so "summarize the beit al muharraq launch plan" REDs on the
    # FREEZE wall, not on aggregation. Same in the shell hook — an accepted over-block
    # of the "make sure the beit storefront is still up" class, trivially rephrased.)
    assert lane("summarize the beit al muharraq positioning") == "GREEN"


# ── R5: NAMING the no-gold wall is not BREACHING it (2026-08-05) ────────────
# `\bgold\b` matches INSIDE "NO-GOLD" because `-` is a word boundary, so text that
# NAMED the rule RED'd as hard as an instruction to break it.
@pytest.mark.parametrize("prompt", [
    "the overlay must not weaken entity isolation / NO-GOLD / freeze-hold detection for beit al muharraq",
    "quote the no-gold rule verbatim in the beit session capture",
    "explain why the no gold wall applies to muharraq packaging",
])
def test_naming_the_wall_is_green(prompt):
    assert lane(prompt) == "GREEN", prompt


# THE WALL ITSELF IS UNCHANGED — a substitution, not an exemption.
@pytest.mark.parametrize("prompt", [
    "make the beit hero gold",
    "redesign the beit al muharraq packaging with gold foil",
    "add a champagne finish to the muharraq label",
    "use a brass motif on the beit storefront",
    "the beit hero needs a warm bronze treatment",
    "give the muharraq gift box an amber-metallic sleeve",
])
def test_real_no_gold_breaches_still_red(prompt):
    assert lane(prompt) == "RED", prompt


def test_substitution_did_not_become_a_bypass():
    """Naming the wall must buy NO silence for a bare `gold` elsewhere in the prompt.

    If either of these goes GREEN, the neutralization has been widened into an exemption.
    """
    assert lane("keep the no-gold rule in mind, then make the beit hero gold anyway") == "RED"
    assert lane("beit is NO-GOLD, so obviously use gold foil on the muharraq label") == "RED"


def test_gold_adjacent_is_not_neutralized():
    """Unlike the rule's name, "gold-adjacent" CAN carry the banned meaning.

    The canon bans gold-adjacent by name, so "use a gold-adjacent finish on the beit
    label" is a real breach. Neutralizing it to make a documentation case pass would
    widen the substitution into the exemption this design avoids. Documenting the wall in
    those exact words costs one banner; that is the right trade.
    """
    assert lane("document that beit al muharraq is NO GOLD including gold-adjacent") == "RED"


# ── RED: other identity walls, which hold even against Ash's instruction ─────
@pytest.mark.parametrize("prompt", [
    "give novra foods a khaleeji register",
    "make franklins feel more gcc",
    "raise a seed round for aevum",
    "add damac to the partner list",
])
def test_other_identity_walls(prompt):
    assert lane(prompt) == "RED", prompt


# ── RED: secrets, including the accepted false positive ─────────────────────
def test_secrets_wall():
    assert lane("print the api key") == "RED"


def test_secrets_false_positive_is_accepted_deliberately():
    """This fires on a sentence that STATES the rule, and that is the accepted trade.

    There is no single token to neutralize (unlike R5's "no-gold") and the only remaining
    lever is a semantic exemption on never/refused/guard — the shape that already became
    a working bypass of the freeze wall. Pinned RED so the trade stays a decision rather
    than drifting into a silent regression.
    """
    assert lane("no secret can ever enter a commit") == "RED"


# ── Obfuscation: every wall is otherwise one homoglyph from open ────────────
@pytest.mark.parametrize("prompt", [
    "add dаmac to the list",          # Cyrillic а
    "add d​amac to the list",         # zero-width space
    "add ＤＡＭＡＣ to the list",             # fullwidth
    "add d.a.m.a.c to the list",           # despaced
])
def test_deobfuscation(prompt):
    assert lane(prompt) == "RED", repr(prompt)


# ── Ordering: an exemption may silence an advisory, never a wall ────────────
def test_question_mark_does_not_disarm_a_wall():
    """Appending '?' turned any reserved act GREEN in the shell hook's v3.0 — a real
    bypass found in audit, because cross-entity aggregation arrives as a question."""
    assert lane("compare beit and franklins revenue?") == "RED"
    assert lane("can you drop the production database?") == "RED"


def test_raw_prefix_suppresses_advice_but_never_a_wall():
    assert lane("raw: deploy to production") == "GREEN"      # advisory suppressed
    assert lane("raw: make the beit hero gold") == "RED"     # wall is not


# ── AMBER: advisory only, and it never stops the work ──────────────────────
@pytest.mark.parametrize("prompt", [
    "deploy the worker to production",
    "force-push to main",
    "buy the domain for 30 usd",
])
def test_amber(prompt):
    assert lane(prompt) == "AMBER", prompt


# ── channel != principal: walls run everywhere, advice does not ─────────────
def test_non_interactive_origin_keeps_walls_but_drops_advice():
    """A webhook replay containing a reserved act must still RED. Suppressing walls for
    machine turns inverted channel != principal: an untrusted channel is exactly the one
    whose content should still be checked."""
    assert lane("make the beit hero gold", interactive=False) == "RED"
    assert lane("deploy the worker to production", interactive=False) == "GREEN"


# ── The banner must not contradict its own matcher ─────────────────────────
def test_banner_names_every_frozen_entity_and_no_unfrozen_one():
    """A session RED'd on Beit once received a banner whose roster excluded Beit — the
    strongest available argument that its own stop was spurious. Observed live."""
    assert "JARVIS" in novra_triage.FROZEN_LIST
    assert "Beit al Muharraq" in novra_triage.FROZEN_LIST
    assert "Novra Foods" not in novra_triage.FROZEN_LIST   # hold LIFTED, ACTIVE
    assert "Franklin" not in novra_triage.FROZEN_LIST
    assert "AEVUM" not in novra_triage.FROZEN_LIST


# ── Hook contract: inject-only, fails open, kill switch ────────────────────
def test_hook_returns_context_for_red():
    out = novra_triage._on_pre_llm_call(user_message="make the beit hero gold")
    assert isinstance(out, dict) and "context" in out
    assert "T0 — STOP AND CLASSIFY" in out["context"]
    # It can only ever contribute context — never block, never rewrite.
    assert set(out) == {"context"}


def test_hook_returns_none_for_green():
    assert novra_triage._on_pre_llm_call(user_message="add a test for the parser") is None
    assert novra_triage._on_pre_llm_call(user_message="") is None


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("NOVRA_TRIAGE_DISABLE", "1")
    assert novra_triage._on_pre_llm_call(user_message="make the beit hero gold") is None


def test_fails_open_on_bad_input():
    # None user_message must not raise into the gateway's turn loop.
    assert novra_triage._on_pre_llm_call(user_message=None) is None


def test_register_wires_pre_llm_call():
    seen = {}

    class Ctx:
        def register_hook(self, name, fn):
            seen[name] = fn

    novra_triage.register(Ctx())
    assert list(seen) == ["pre_llm_call"]
