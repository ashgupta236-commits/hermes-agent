"""Temporal truth maintenance: ABSENT(t0) -> CREATED(t1) -> PRESENT(t2) is not a contradiction.

A claim about mutable state is true *of a moment*. "calc.py does not exist" was accurate when it
was observed; after the runtime created the file it did not become false, it became **historical**.
Treating it as an eternal proposition is what produced a severity-1.0 contradiction in the live run
and drove `must_falsify` on three separate cycles, spending 48% of the mission budget re-litigating
a question the filesystem could have answered in a millisecond.

Two mechanisms, both deterministic:

* :func:`subjects_of` extracts the things a proposition asserts the current state of — path-like
  tokens, generically, with no knowledge of any particular file or domain.
* :class:`TemporalSettler` settles a contradiction by **looking**, before any frontier cognition is
  spent on it. If the disputed claims are about directly observable current state, it observes that
  state now through the tool fabric, supersedes the claims the observation has overtaken, and
  resolves the contradiction with the period under which each was true.

History is never destroyed. A superseded claim stays in mission state with `superseded_by` pointing
at what replaced it, so "what did we believe at t0, and why" remains answerable.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from cogos.ids import iso_now
from cogos.schemas.beliefs import Claim, ClaimStatus, Contradiction
from cogos.schemas.tools import ToolCall

#: Tokens that look like a filesystem path. Deliberately general: any path-like string, not a
#: list of interesting filenames.
_PATH_RE = re.compile(r"(?:(?:~|\.{1,2})?/)?(?:[\w.\-]+/)*[\w.\-]+\.[A-Za-z0-9]{1,8}\b|(?:/[\w.\-]+)+/?")

#: Wording that asserts something about state *now* rather than a timeless proposition.
_STATE_RE = re.compile(
    r"\b(exists?|existed|does not exist|doesn't exist|is (?:present|absent|missing|empty)|are (?:present|absent|missing)|"
    r"not found|no longer|now (?:exists?|contains?|present)|was (?:created|written|deleted|removed)|"
    r"file set|contains?|currently|baseline|already)\b",
    re.IGNORECASE,
)

#: Wording that says the subject is absent. Used to compare a claim against what is observed.
_ABSENT_RE = re.compile(
    r"\b(do(?:es)? not exist|doesn'?t exist|not exist|is absent|are absent|is missing|are missing|"
    r"not found|no longer exists?|deleted|removed|absent)\b",
    re.IGNORECASE,
)


def subjects_of(proposition: str) -> list[str]:
    """Normalised identifiers of what a proposition asserts the current state of.

    Path-like tokens are reduced to their basename so that a claim naming a bare `calc.py` and one
    naming `/tmp/run/calc.py` are recognised as being about the same thing. Nothing here knows any
    particular filename — it is the *shape* of a path that matters.
    """
    out: list[str] = []
    for match in _PATH_RE.findall(proposition or ""):
        token = match.strip().rstrip("/.,;:)")
        if not token or token in (".", ".."):
            continue
        name = Path(token).name
        if name and "." in name and name not in out:
            out.append(name)
    return out


def asserts_current_state(proposition: str) -> bool:
    """Whether the proposition is about how things are *now* rather than a timeless fact."""
    return bool(_STATE_RE.search(proposition or "")) and bool(subjects_of(proposition))


def classify_claim(claim: Claim) -> Claim:
    """Stamp a claim with its temporal scope. Idempotent, deterministic, no model involved."""
    if not claim.subjects:
        claim.subjects = subjects_of(claim.proposition)
    if not claim.observes_current_state:
        claim.observes_current_state = asserts_current_state(claim.proposition)
    if claim.observed_at is None:
        claim.observed_at = claim.created_at
    return claim


def supersede(old: Claim, new: Claim, reason: str = "") -> Claim:
    """Mark `old` as overtaken by `new`. The claim is kept, not deleted."""
    old.superseded_by = new.id
    old.superseded_at = iso_now()
    old.status = ClaimStatus.STALE
    old.updated_at = iso_now()
    if reason:
        note = f"superseded: {reason}"
        if note not in old.assumptions:
            old.assumptions.append(note)
    return old


def supersede_overtaken(state: Any, new_claim: Claim) -> list[Claim]:
    """Supersede live current-state claims that `new_claim` overtakes on the same subject.

    Only claims that (a) assert current state, (b) share a subject, and (c) were observed earlier
    are affected — a timeless proposition that merely mentions the same file is untouched.
    """
    classify_claim(new_claim)
    if not new_claim.observes_current_state or not new_claim.subjects:
        return []
    overtaken: list[Claim] = []
    subjects = set(new_claim.subjects)
    for claim in state.claims:
        if claim.id == new_claim.id or not claim.live():
            continue
        classify_claim(claim)
        if not claim.observes_current_state or not subjects & set(claim.subjects):
            continue
        if (claim.observed_at or claim.created_at) > (new_claim.observed_at or new_claim.created_at):
            continue  # the existing claim is the newer observation
        supersede(claim, new_claim, reason=f"a later observation of {', '.join(sorted(subjects & set(claim.subjects)))} replaced it")
        overtaken.append(claim)
    return overtaken


class SettlementResult:
    def __init__(self, settled: bool, resolution: str, observed: dict[str, bool], superseded: list[str], cost_free: bool = True):
        self.settled = settled
        self.resolution = resolution
        self.observed = observed
        self.superseded = superseded
        self.cost_free = cost_free


class TemporalSettler:
    """Settles a contradiction by observing current state, before any cognition is spent on it.

    This is the escalation principle applied to disputes: if the question is "does this file exist",
    the authoritative answer is a filesystem call, not a frontier deliberation. Only disputes that
    survive this cheap check are worth escalating to falsification.
    """

    def __init__(self, fabric: Any, tracer: Any = None):
        self.fabric = fabric
        self.tracer = tracer

    def observable_subjects(self, contradiction: Contradiction, state: Any) -> list[str]:
        subjects: list[str] = []
        for cid in contradiction.claim_ids:
            claim = state.claim(cid)
            if claim is None:
                continue
            classify_claim(claim)
            for subject in claim.subjects:
                if subject not in subjects:
                    subjects.append(subject)
        return subjects

    def _observe(self, subject: str, roots: list[str]) -> Optional[bool]:
        """Does `subject` exist right now? None when it cannot be determined cheaply."""
        if self.fabric is None:
            return None
        for root in roots:
            res = self.fabric.execute(
                ToolCall(tool="list_dir", arguments={"path": root, "glob": subject}, purpose="temporal_settlement")
            )
            if not res.ok:
                continue
            entries = res.data.get("entries") if isinstance(res.data, dict) else None
            haystack = " ".join(map(str, entries)) if entries else str(res.output or "")
            if subject in haystack:
                return True
        return False

    def settle(self, contradiction: Contradiction, state: Any, roots: Optional[list[str]] = None) -> SettlementResult:
        """Try to resolve the contradiction from current state alone."""
        subjects = self.observable_subjects(contradiction, state)
        if not subjects:
            return SettlementResult(False, "no directly observable subject in the disputed claims", {}, [])

        search_roots = list(roots or [])
        if not search_roots:
            root = getattr(getattr(self.fabric, "context", None), "workdir", None)
            search_roots = [str(root)] if root else ["."]

        observed: dict[str, bool] = {}
        for subject in subjects:
            present = self._observe(subject, search_roots)
            if present is None:
                return SettlementResult(False, f"could not observe '{subject}' cheaply", observed, [])
            observed[subject] = present

        # Supersede every disputed claim the current observation has overtaken.
        superseded: list[str] = []
        for cid in contradiction.claim_ids:
            claim = state.claim(cid)
            if claim is None or not claim.live():
                continue
            classify_claim(claim)
            if not claim.observes_current_state:
                continue
            says_absent = bool(_ABSENT_RE.search(claim.proposition))
            relevant = [s for s in claim.subjects if s in observed]
            if not relevant:
                continue
            # Evaluate per subject, not as a conjunction. A compound claim ("baseline is {A};
            # B and C do not exist") is overtaken the moment ANY subject it asserts as absent is
            # present now — requiring all of them would let one unrelated subject mask the change.
            if says_absent:
                overtaken_now = any(observed[s] for s in relevant)
            else:
                overtaken_now = any(not observed[s] for s in relevant)
            if overtaken_now:
                claim.status = ClaimStatus.STALE
                claim.superseded_at = iso_now()
                claim.observed_at = claim.observed_at or claim.created_at
                claim.updated_at = iso_now()
                seen = ", ".join(f"{s}={'present' if observed[s] else 'absent'}" for s in relevant)
                note = f"superseded by direct observation at {iso_now()}: {seen}"
                if note not in claim.assumptions:
                    claim.assumptions.append(note)
                superseded.append(claim.id)

        if not superseded:
            return SettlementResult(False, "current state does not overtake either claim; the dispute is genuine", observed, [])

        state_desc = ", ".join(f"{s} is {'present' if v else 'absent'}" for s, v in sorted(observed.items()))
        resolution = (
            f"settled by direct observation of current state ({state_desc}); "
            f"{len(superseded)} claim(s) were true of an earlier moment and are superseded, not refuted"
        )
        contradiction.resolved = True
        contradiction.resolution = resolution[:600]
        contradiction.suspected_cause = "time_period"
        if self.tracer is not None:
            self.tracer.emit(
                "verify",
                f"contradiction {contradiction.id} settled deterministically: {state_desc}",
                data={"contradiction_id": contradiction.id, "observed": observed, "superseded_claims": superseded},
            )
        return SettlementResult(True, resolution, observed, superseded)
