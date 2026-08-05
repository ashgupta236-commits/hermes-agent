"""NOVRA Tier-0 prompt triage — the Hermes-native counterpart of the Claude Code hook.

WHY THIS EXISTS AS A PORT RATHER THAN A COPY.

``novra-os/.claude/hooks/prompt-triage.sh`` runs on Claude Code's ``UserPromptSubmit``
event: the harness executes the script and injects its stdout into the turn. Hermes has
no inbound hook-script mechanism — the only ``UserPromptSubmit`` references in this repo
are OUTBOUND telemetry in ``plugins/platforms/raft/adapter.py``, which report that a
prompt happened rather than run anything. So the shell hook cannot execute here at any
price, and shipping it to the box as reference prose would look like a deployment while
behaving like a document.

What Hermes does have is ``pre_llm_call``, and it is a near-exact structural match:
``agent/turn_context.py`` passes ``user_message`` (the prompt text) and returns any
``{"context": ...}`` a plugin yields **injected into the user message**. That is the same
inject-only contract the shell hook relies on, so the lane semantics port faithfully.

THREE LANES, unchanged from the shell v3.1 — the asymmetry is deliberate:

  GREEN (default)  no injection. The canon already governs and T2 is act-then-report;
                   silence here is the point, not a gap.
  AMBER            a PLAN-FIRST advisory. No approval wait — an advisory, not a gate.
  RED (T0-class)   reserved act / frozen surface / cross-entity aggregation / secrets /
                   identity wall. Halt, fail closed, surface. This lane keeps the stop.

INVARIANTS, all inherited on purpose:

  * INJECT-ONLY. This hook returns context or nothing. It can never block a prompt,
    rewrite one, or refuse a turn. The reserved-acts rail and the firm gate are the real
    enforcement; this is a lane sorter, never a wall.
  * CHANNEL != PRINCIPAL, and Hermes makes this sharper than Claude Code can. Hermes
    knows the ``platform`` a turn arrived on (Slack, Discord, webhook, cron). Walls are
    therefore evaluated on EVERY platform without exception — an untrusted channel is
    precisely the one whose content still needs checking. Only the ADVISORY lane is
    suppressed for non-interactive origins, because advice exists to steer a human author
    and a cron has nobody to read it.
  * FAILS OPEN. Any exception yields no context. A triage outage must never break the
    gateway's ability to answer. The caller in ``turn_context.py`` also wraps this in
    try/except, so this is belt and braces.
  * NEVER LOGS PROMPT TEXT. The lane is one word and leaks nothing; the prompt may
    contain a secret and writing it to disk is exactly what the secrets wall forbids.

Kill switch: ``NOVRA_TRIAGE_DISABLE=1``.
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Normalization — de-obfuscation, ported from the shell hook's embedded python.
# Every wall is otherwise one homoglyph from open: "dаmac" with a Cyrillic а,
# fullwidth "ＤＡＭＡＣ", and "d a m a c" all read as the banned word to a human and
# match nothing. Three views are produced and the walls grep their concatenation, so a
# match in ANY view fires. This closes the transcription hole, which is the one an
# ordinary paste actually hits. It cannot close the semantic hole — a description of a
# banned thing that never spells it — and nothing at Tier-0 can.
# ─────────────────────────────────────────────────────────────────────────────

_CONFUSABLES = {
    0x0430: "a", 0x0435: "e", 0x043E: "o", 0x0440: "p", 0x0441: "c", 0x0443: "y",
    0x0445: "x", 0x0456: "i", 0x0455: "s", 0x043C: "m", 0x0432: "b", 0x043D: "h",
    0x0501: "d", 0x04BB: "h", 0x0410: "a", 0x0415: "e", 0x041E: "o", 0x0420: "p",
    0x0421: "c", 0x0423: "y", 0x0425: "x", 0x041C: "m", 0x0412: "b", 0x041D: "h",
    0x0406: "i", 0x0405: "s", 0x041A: "k", 0x0422: "t", 0x03B1: "a", 0x03B5: "e",
    0x03BF: "o", 0x03C1: "p", 0x03C4: "t", 0x03BD: "v", 0x03BA: "k", 0x03C5: "u",
    0x03B9: "i", 0x0391: "a", 0x0395: "e", 0x039F: "o", 0x03A1: "p", 0x03A4: "t",
    0x039A: "k", 0x0392: "b", 0x0397: "h", 0x0399: "i", 0x039C: "m", 0x039D: "n",
    0x0131: "i", 0x0130: "i", 0x0142: "l", 0x00F8: "o", 0x0433: "r", 0x0413: "r",
    0x043F: "n", 0x041F: "n", 0x043B: "n", 0x0438: "u", 0x0418: "u", 0x044F: "r",
    0x042F: "r", 0x0446: "u", 0x0459: "n", 0x04CF: "i", 0x0261: "g", 0x0269: "i",
}
_LEET = {ord("4"): "a", ord("3"): "e", ord("0"): "o", ord("1"): "i",
         ord("5"): "s", ord("7"): "t", ord("8"): "b"}
_SEQ = re.compile(r"(?:(?<=\s)|^)(?:[a-z0-9][ .\-_]+){2,}[a-z0-9](?=\s|$)")

# Above this size the extra views are DROPPED rather than the text truncated.
# Truncating would create a real bypass (hide the act past the cutoff); dropping the
# de-obfuscation views only loses coverage of hand-typed evasion, which is short by
# nature. The folded view always covers the whole prompt.
_VIEW_LIMIT = 262_144
# Longer than the widest proximity window, so no bounded pattern can match across the
# boundary between views. A character separator can never do this job: whatever stops
# the separator also stops in ordinary prose.
_SEP = " " + ("z" * 60) + " "


def _flat(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\n", " ").replace("\t", " ").replace("\r", " ")).strip()


def _fold(s: str) -> str:
    s = "".join(c for c in s if unicodedata.category(c) != "Cf")
    s = unicodedata.normalize("NFKC", s)
    s = "".join(c for c in unicodedata.normalize("NFD", s)
                if unicodedata.category(c) != "Mn")
    return s.translate(_CONFUSABLES).lower()


def build_views(prompt: str) -> tuple[str, str]:
    """Return ``(folded, wall)`` — the plain lowercase view and the multi-view haystack."""
    folded = _flat(_fold(prompt or ""))
    if len(folded) > _VIEW_LIMIT:
        return folded, folded
    despaced = _SEQ.sub(lambda m: re.sub(r"[ .\-_]+", "", m.group(0)), folded)
    leet = folded.translate(_LEET)
    return folded, folded + _SEP + despaced + _SEP + leet


def _has(hay: str, pattern: str) -> bool:
    return re.search(pattern, hay, re.IGNORECASE) is not None


# ─────────────────────────────────────────────────────────────────────────────
# RED — T0-class signals. Evaluated FIRST and returned immediately, before any
# exemption can run. An exemption may silence an advisory; it may never silence a wall.
# (The shell hook's v3.0 ran the question/ack skips first, so appending a single "?"
# turned any reserved act GREEN — a real bypass, found in audit. Same ordering here.)
# ─────────────────────────────────────────────────────────────────────────────

# ARMED frozen surfaces. Changing this list either way is reserved act #8 and needs
# Ash's explicit in-message instruction. 'beit|muharraq' is ONE alternation on purpose:
# as two entries it would double-count in the entity tally below and false-RED ordinary
# single-entity work.
_FROZEN_HARD = r"\bjarvis\b|cold[ _-]?storage|\bbeit\b|\bmuharraq\b"
_FROZEN_PATH = r"\bdashboard/"
# Lives beside the matcher and moves with it. A regression asserts this names every
# frozen entity and no unfrozen one — a banner that contradicts its own matcher hands a
# session the strongest available argument that its stop was spurious.
FROZEN_LIST = "JARVIS, Beit al Muharraq"

_EDIT_VERB = (r"fix|change|update|add|remove|delete|edit|build|write|rewrite|refactor|ship|"
              r"publish|deploy|launch|design|redesign|revamp|wake|unfreeze|lift|restore|"
              r"enable|create|make|generate|render|draft|mock|mockup|prototype")
_DOC_NOUN = r"wiki|session log|session capture|vault|decision memo|adr|changelog|notes?|document(ing|ation)?"

_ENTITY_TOKENS = (r"beit|muharraq", r"franklin", r"aevum", r"jarvis", r"palmsip", r"novra[ -]?foods")


def classify(prompt: str, interactive: bool = True) -> tuple[str, str]:
    """Return ``(lane, kind)`` — lane in {GREEN, AMBER, RED}.

    ``interactive`` gates only the ADVISORY lane. Walls are evaluated regardless, because
    channel != principal: an untrusted origin is exactly the one whose content still
    needs checking.
    """
    lc, wall = build_views(prompt)
    if not wall:
        return "GREEN", ""
    trimmed = lc.lstrip()

    # Code-context guard. These nouns collide hard with money/legal vocabulary in
    # ordinary dev work ("wire up the payment form", "the agreement parser"). It gates
    # ONLY the three rules whose vocabulary genuinely collides — never the whole block.
    # Gating everything made 22 everyday nouns a user-typeable kill switch for reserved
    # acts, which is how a webhook replay containing "config" disarmed R1.
    code_ctx = _has(lc, r"\b(mock|fixture|stub|util|utils|parser|component|module|class|schema|"
                        r"test suite|unit test|config|helper|refactor|import|variable|function|"
                        r"typescript|css|lint)\b") or _has(lc, r"wir(e|ing)[ -]up|sign(ed)?[ -]off|sign off on")

    # R1 — reserved acts.
    if not code_ctx:
        if (_has(wall, r"\b(wire|transfer|pay|payout|remit|disburse|withdraw|send|settle|release|move)\b")
                and _has(wall, r"(money|funds?|capital|invoice|payment|salary|wages|deposit|\$[0-9]|"
                               r"[0-9]+[ ]?(usd|bhd|inr|aed|sar)|\b(usd|bhd|inr)\b)")
                and _has(wall, r"(\$[0-9]|[0-9]{2,}|to the (vendor|supplier|co-?packer|contractor|agency|"
                               r"account|bank)|the (vendor|supplier|co-?packer)\b|get paid|paid out)")):
            return "RED", "reserved-act (money)"
        if _has(wall, r"(\b(sign|execute|countersign|finalize|finalise|accept|agree to)\b|\bput\b.*\bthrough\b)"
                      r".*(contract|agreement|loi|term sheet|licen[cs]e|nda|lease|msa|sow)|"
                      r"\b(contract|agreement|loi|nda)\b.*\b(signed|countersigned|executed|through)\b"):
            return "RED", "reserved-act (legal bind)"
        if _has(wall, r"\b(incorporate|register|dissolve|liquidate|form|set up|wind down|open)\b.*"
                      r"\b(entity|compan(y|ies)|llc|ltd|wll|spc|freezone|bank account|emi account)\b"):
            return "RED", "reserved-act (entity/banking)"
    # Speak-as-Ash and live-money never collided with dev vocabulary — unconditional.
    if _has(wall, r"(as ash|on ash.s behalf|from ash.s (account|handle)|speak as ash)|"
                  r"\b(press release|to the press|mass audience)\b|"
                  r"\b(publish|announce|blast|email)\b.*\bnewsletter\b"):
        return "RED", "reserved-act (speak as Ash / mass publish)"
    # Verb and object checked INDEPENDENTLY of order: "take the prod db offline" puts the
    # object before the particle, so a single ordered pattern misses it.
    if (_has(wall, r"\b(drop|destroy|wipe|delete|truncate|decommission|nuke|retire)\b|tear\b.*\bdown\b|"
                   r"rm -rf|\btake\b.*\boffline\b|\bpermanently\b")
            and _has(wall, r"\b(prod|production|live)\b")
            and _has(wall, r"\b(database|db|prod|production|s3|bucket|volume|instance|backup|cluster)\b")):
        return "RED", "reserved-act (destroy prod)"
    if _has(wall, r"firm_live[\s=]*1|turn (on|the firm to) live|flip .*to live money|live[ -]money mode"):
        return "RED", "reserved-act (live money)"

    # R2b — CHANGING freeze/hold state is reserved act #8 itself, independent of whether
    # any surface is currently frozen. This is the rule, not the status: it does not
    # expire when a hold is lifted, and the next "unfreeze X" must stop exactly as this
    # one did.
    #
    # ORDERED BEFORE R2 DELIBERATELY. "wake-jarvis" satisfies both rules — it names a
    # frozen surface AND changes freeze state — and #8 is the more specific, more serious
    # reading. The shell hook evaluates every rule and lets the last match win, which
    # lands on #8 for the same input; returning early on R2 would report "frozen-surface"
    # instead and drift from it. The lane is RED either way; the KIND is what a reader
    # acts on, so it has to agree across the two implementations.
    if _has(wall, r"\b(unfreeze|re-?freeze|thaw|reactivate|un-?hold)\b|"
                  r"\b(lift|remove|release|end)\b.{0,30}\b(the )?(freeze|hold|cold[ _-]?storage)\b|"
                  r"\b(freeze|put on hold|place on hold)\b.{0,30}\b(jarvis|dashboard|surface|site|entity|repo|project)\b|"
                  r"\b(put|place|move|set|back)\b.{0,40}\bon hold\b|wake[ -]jarvis"):
        return "RED", "reserved-act (freeze/hold change — #8)"

    # R2 — edit verbs aimed at a FROZEN surface.
    # The doc exemption is ANCHORED to the verb's OBJECT, never merely present. As a
    # whole-prompt check it was a working bypass: any prompt containing "wiki" anywhere
    # disarmed the freeze entirely. Canon §8 MANDATES a session capture that will name the
    # frozen surface, so the exemption is real and must survive — but only when a doc noun
    # is the verb's actual object, within three words.
    doc_exempt = _has(wall, rf"\b({_EDIT_VERB})\b(\s+[\w'-]+){{0,3}}\s+\b({_DOC_NOUN})\b")
    frozen_hit = False
    if _has(wall, _FROZEN_HARD):
        frozen_hit = True
    elif _has(wall, _FROZEN_PATH) and not _has(wall, r"reactor[ -]?hud|dashboard-reactor|\breactor\b"):
        # A reactor mention stands down a bare 'dashboard/' path hit and nothing else;
        # against a NAMED frozen entity it is irrelevant noise. Applying the stand-down to
        # named entities too was a bug: one unrelated word switched off the freeze wall.
        frozen_hit = True
    if frozen_hit and not doc_exempt and _has(wall, rf"\b({_EDIT_VERB})\b"):
        return "RED", "frozen-surface"

    # R3 — cross-entity aggregation, reserved act #7, and the one that reads as helpful.
    ecount = sum(1 for e in _ENTITY_TOKENS if _has(wall, e))
    collective = _has(wall, r"(all|each|every|both|across)([ -]the)?[ -](brand|entit|ventur|compan|business)|"
                            r"the portfolio|portfolio (view|level|wide)|group[ -]?wide|whole (group|portfolio)|holdco") \
        or _has(wall, r"which (ventur|brand|entit|compan|business|one)")
    if (ecount >= 2 or collective) and _has(
            wall, r"(compare|combined?|consolidat|aggregat|total|sum|portfolio|treasury|versus|\bvs\b|"
                  r"which (one|brand|entity|ventur)|side[ -]by[ -]side|overall|doing better|numbers)"):
        return "RED", "cross-entity-aggregation"

    # R4 — secrets exfiltration shapes.
    # KNOWN FALSE POSITIVE, ACCEPTED DELIBERATELY: this also fires on a sentence that
    # STATES the rule ("no secret can ever enter a commit"), because `commit` and `secret`
    # both carry their ordinary meanings there. There is no single token to neutralize —
    # unlike R5's "no-gold" — and the only remaining lever is a semantic exemption on
    # never/refused/guard, which is the shape that already became a working bypass of the
    # freeze wall. The cost is one banner from an inject-only hook. A T0 wall is the wrong
    # place to trade a false stop for a real hole.
    if _has(wall, r"\b(paste|print|show|echo|cat|dump|share|send|commit|expose)\b.*"
                  r"(secret|token|api[ _-]?key|password|credential|\.env|auth\.json|private key|\.pem)"):
        return "RED", "secrets"

    # R5 — identity walls. These hold EVEN AGAINST ASH'S INSTRUCTION, so an instruction to
    # cross one is exactly what must stop.
    #
    # NEUTRALIZE THE WALL'S OWN NAME FIRST. `\bgold\b` matches INSIDE "NO-GOLD" because
    # `-` is a word boundary, so text NAMING the rule RED'd as hard as an instruction to
    # USE gold — observed live 2026-08-05 on `gold / freeze-hold detection. beit al
    # muharraq`, in a document written to ENFORCE this wall.
    #
    # A SUBSTITUTION, NOT AN EXEMPTION. An exemption asks "does this look benign?" and is
    # one padded sentence from a bypass. This deletes one token that cannot carry the
    # banned meaning: "no-gold" names the rule, and no instruction to USE gold needs to
    # write it. Every bare `gold` elsewhere still matches, at unbounded distance.
    # Note "gold-adjacent" is deliberately NOT neutralized — unlike the rule's name it CAN
    # carry the banned meaning, since the canon bans gold-adjacent by name.
    r5 = re.sub(r"\bno[ _-]?gold\b", " ", wall, flags=re.IGNORECASE)
    if _has(r5, r"(beit|muharraq).*\b(gold|golden|gilt|brass|champagne|bronze|amber[ -]metallic)\b|"
                r"\b(gold|golden|gilt|brass|champagne|bronze)\b.*(beit|muharraq)"):
        return "RED", "identity-wall (Beit no-gold)"
    if _has(wall, r"novra[ -]?foods.*(khaleeji|arabic register|beit.s (palette|register))|"
                  r"franklin.*(gcc|gulf|khaleeji)|"
                  r"aevum.*\b(equity|dilut|raise|seed round|cap table|investor round)\b|"
                  r"\b(equity|dilut|seed round|cap table|raise a (seed|series|round))\b.*aevum"):
        return "RED", "identity-wall"
    if _has(wall, r"\bdamac\b"):
        return "RED", "identity-wall (DAMAC)"

    # ── Advisory lane only, from here down. ──
    # A non-interactive origin skips advice because advice exists to steer a HUMAN author.
    # It never reaches this line without having been through the wall block above.
    if not interactive:
        return "GREEN", ""

    if trimmed.startswith("raw:") or trimmed.startswith("/"):
        return "GREEN", ""
    # A genuine question: true interrogative opener AND a closing '?'.
    if re.match(r"^(what|why|how|who|whom|whose|which|when|where|is|are|was|were|does|did|do)\b.*\?\s*$",
                trimmed, re.IGNORECASE):
        return "GREEN", ""
    ack = re.sub(r"[\s!.?…,]+$", "", trimmed)
    if re.fullmatch(r"(y|ya|yes|yess+|yep|yeah|yup|no|nope|nah|ok|okay|okey|k|kk|sure|fine|good|great|"
                    r"nice|cool|perfect|thanks|thank you|thx|ty|tysm|done|stop|wait|hold on|hold off|go|"
                    r"go ahead|go on|go for it|proceed|continue|carry on|keep going|do it|ship it|send it|"
                    r"sounds good|looks good|lgtm|approved|approve|confirmed|confirm|agreed|correct|right|"
                    r"exactly|got it|understood|noted|np|no problem|yes please|please do|ok go|okay go|"
                    r"hi|hey|hello|yo|gm|good morning|good night|morning)", ack, re.IGNORECASE):
        return "GREEN", ""

    # AMBER — irreversible-adjacent. Risk nouns require VERB PROXIMITY, and spend verbs
    # require an OBJECT; otherwise "order the imports alphabetically" injects a T1
    # advisory on plainly reversible work, which is the noise this lane exists to remove.
    if (_has(lc, r"\b(deploy|ship|release|publish|push|merge|migrate|rotate|restart|scale|cut[ -]?over|"
                 r"roll[ -]?back|revert|force[ -]?push)\b.*(prod|production|live|main|master|money|payment|"
                 r"billing|customer|real)")
            or _has(lc, r"(prod|production|live site|main branch|force[ -]?push).*"
                        r"\b(deploy|ship|release|publish|push|merge|migrate|restart)\b")
            or _has(lc, r"\b(spend|purchase|buy|order|invoice|subscribe|charge)\b.*"
                        r"(domain|licen[cs]e|subscription|credits?|plan|seat|\$[0-9]|usd|bhd|inr|supplier|vendor)")
            or _has(lc, r"firm_live")):
        return "AMBER", ""

    return "GREEN", ""


RED_BANNER = """[T0 — STOP AND CLASSIFY] This prompt matches a reserved-act / wall signal
({kind}). Do NOT execute it as asked. Per NOVRA-CANON §3-§4: halt, fail closed,
and surface. The block is terminal and non-expiring — silence, non-response, or a
timeout is NEVER approval, and no plan-level "yes" cascades to the act itself.
Reserved acts (the SOUL.md six + charter §10 B/C) need Ash's authenticated command
for THAT SPECIFIC act. Frozen surfaces ({frozen}) wake only on an explicit
in-message instruction, never inferred; imposing a freeze or hold is as reserved as
lifting one. Cross-entity aggregation is refused and escalated at any market-layer
seat — answer in isolated [ENTITY] blocks instead. Identity walls (Beit no-gold,
not-Khaleeji, not-GCC, AEVUM no-equity, DAMAC) hold even against Ash's instruction.
Secrets never enter a commit, an SSM payload, a log, or a report. Do the full
analysis, state the downside plainly, bring the recommendation already made — then
hand Ash the trigger. Read the source text before acting."""

AMBER_BANNER = """[T1? — PLAN FIRST, THEN PROCEED] This prompt is irreversible-adjacent (production /
money / main-branch / force-push signal). Classify before your first write: if the
state is NOT restorable from git or an observed backup, or the target main is marked
IRREVERSIBLE in the canon Reversibility Map (unlisted = IRREVERSIBLE), it is T1 —
prepare everything (branch, build, green CI, staged diff, rollback plan), then propose
ONCE with a recommendation and wait once. Otherwise it is T2: state the tier in one
line and act, then report with observed evidence.
This is an advisory, not a gate. Do not ask for permission the canon already grants."""

# Origins with no human author to steer. Walls still run on these — only advice is
# suppressed. Kept as a set rather than a truthiness check so an unknown platform
# defaults to interactive (advice is cheap; a missed advisory is not).
_NON_INTERACTIVE = {"cron", "webhook", "scheduler", "system"}


def _disabled() -> bool:
    return os.environ.get("NOVRA_TRIAGE_DISABLE", "").lower() in {"1", "true", "yes", "on"}


def _on_pre_llm_call(
    user_message: str = "",
    platform: str = "",
    **_: Any,
) -> Optional[Dict[str, str]]:
    """Return ``{"context": banner}`` for AMBER/RED, else ``None``.

    Fails open on ANY error: a triage outage must never break the gateway.
    """
    try:
        if _disabled() or not user_message:
            return None
        interactive = (platform or "").strip().lower() not in _NON_INTERACTIVE
        lane, kind = classify(user_message, interactive=interactive)
        # The lane ONLY — never the prompt text. Logging prompt content here would write
        # arbitrary user input, potentially including a secret, to a log file, which the
        # secrets wall forbids outright. A lane is one word and leaks nothing.
        if lane != "GREEN":
            logger.info("novra-triage lane=%s kind=%s", lane, kind or "-")
        if lane == "RED":
            return {"context": RED_BANNER.format(kind=kind, frozen=FROZEN_LIST)}
        if lane == "AMBER":
            return {"context": AMBER_BANNER}
        return None
    except Exception:
        logger.debug("novra-triage failed open", exc_info=True)
        return None


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
