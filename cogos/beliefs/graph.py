"""Belief graph operating in place on a :class:`MissionState`.

The graph is deliberately deterministic: no model is consulted. It aggregates
evidence into claim confidence with a log-odds scheme, discounts evidence that
shares a lineage root with evidence already counted (five sites repeating one
report count roughly once), detects contradictions, marks stale claims and runs
hypothesis tournaments. Epistemic status is never changed automatically.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from cogos.governance.immune import normalise_source_key, source_trust
from cogos.ids import iso_now
from cogos.schemas.beliefs import Claim, ClaimStatus, Contradiction, Evidence, EvidenceKind, Hypothesis
from cogos.schemas.common import EpistemicStatus
from cogos.schemas.mission import MissionState

KIND_FACTOR: dict[EvidenceKind, float] = {
    EvidenceKind.PRIMARY: 1.0,
    EvidenceKind.SECONDARY: 0.7,
    EvidenceKind.TERTIARY: 0.4,
}
SHARED_ROOT_DISCOUNT = 0.15
LOG_ODDS_SCALE = 2.5
CONFIDENCE_MIN = 0.02
CONFIDENCE_MAX = 0.98
FUZZY_THRESHOLD = 0.6
CONTEST_WEIGHT = 0.3
DEFAULT_RELIABILITY = 0.5
EXCLUSIVE_PREFIX = "exclusive_with:"
STALE_PULL = 0.3


# --- small helpers -------------------------------------------------------------


def normalise_proposition(text: str) -> str:
    """Case/whitespace-insensitive key with trailing periods stripped."""
    collapsed = re.sub(r"\s+", " ", (text or "").strip().lower())
    return collapsed.rstrip(". ").strip()


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _clamp(value: float, low: float = CONFIDENCE_MIN, high: float = CONFIDENCE_MAX) -> float:
    return max(low, min(high, value))


def _logit(p: float) -> float:
    p = _clamp(p)
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class _Side:
    """Aggregate of one side (for/against) of a claim."""

    weight: float = 0.0
    clusters: int = 0
    max_reliability: float = 0.0
    evidence: list[Evidence] = field(default_factory=list)


class TournamentResult(BaseModel):
    question: str = ""
    ranked: list[dict[str, Any]] = Field(default_factory=list)
    leading_id: Optional[str] = None
    margin: float = 0.0
    discriminating_predictions: list[str] = Field(default_factory=list)
    recommended_falsification: str = ""


class BeliefGraph:
    """Operates in place on ``state.claims``, ``state.evidence``, ``state.hypotheses`` and ``state.contradictions``."""

    def __init__(self, state: MissionState):
        self.state = state
        # Starting log-odds per claim, taken from the confidence a claim had before any evidence arrived.
        self._priors: dict[str, float] = {}
        self._exclusive: set[frozenset[str]] = set()
        for claim in state.claims:
            if not claim.evidence_for and not claim.evidence_against:
                self._priors[claim.id] = claim.confidence
            for marker in claim.assumptions:
                if marker.startswith(EXCLUSIVE_PREFIX):
                    other = marker[len(EXCLUSIVE_PREFIX) :].strip()
                    if other:
                        self._exclusive.add(frozenset({claim.id, other}))

    # --- claims ----------------------------------------------------------------

    def add_claim(self, claim: Claim) -> Claim:
        key = normalise_proposition(claim.proposition)
        for existing in self.state.claims:
            if existing.id == claim.id or normalise_proposition(existing.proposition) == key:
                for cond in claim.falsification_conditions:
                    if cond not in existing.falsification_conditions:
                        existing.falsification_conditions.append(cond)
                for assumption in claim.assumptions:
                    if assumption not in existing.assumptions:
                        existing.assumptions.append(assumption)
                existing.updated_at = iso_now()
                return existing
        self.state.claims.append(claim)
        self._priors.setdefault(claim.id, claim.confidence)
        self._recompute_claim(claim)
        return claim

    def find_claim(self, text_or_id: str) -> Optional[Claim]:
        if not text_or_id:
            return None
        found = self.state.claim(text_or_id)
        if found is not None:
            return found
        key = normalise_proposition(text_or_id)
        for claim in self.state.claims:
            if normalise_proposition(claim.proposition) == key:
                return claim
        query = _tokens(text_or_id)
        best: Optional[Claim] = None
        best_score = 0.0
        for claim in self.state.claims:
            score = _jaccard(query, _tokens(claim.proposition))
            if score > best_score:
                best, best_score = claim, score
        if best is not None and best_score >= FUZZY_THRESHOLD:
            return best
        return None

    # --- evidence --------------------------------------------------------------

    def _resolve_claim_refs(self, refs: list[str], create_missing: bool) -> list[str]:
        resolved: list[str] = []
        for ref in refs:
            if not ref or not ref.strip():
                continue
            claim = self.find_claim(ref)
            if claim is None:
                if not create_missing or ref.startswith("clm_"):
                    continue
                claim = Claim(proposition=ref.strip(), epistemic_status=EpistemicStatus.HYPOTHESIS)
                claim = self.add_claim(claim)
            if claim.id not in resolved:
                resolved.append(claim.id)
        return resolved

    @staticmethod
    def _root_keys(ev: Evidence) -> set[str]:
        keys = {normalise_source_key(r) for r in ev.root_sources() if r and r.strip()}
        if ev.provenance.source and ev.provenance.source.strip():
            keys.add(normalise_source_key(ev.provenance.source))
        return {k for k in keys if k}

    def _shares_root(self, a: Evidence, b: Evidence) -> bool:
        if a.id in b.independent_of or b.id in a.independent_of:
            return False
        return bool(self._root_keys(a) & self._root_keys(b))

    def add_evidence(self, ev: Evidence) -> Evidence:
        existing = self.state.evidence_item(ev.id)
        if existing is not None:
            return existing
        ev.supports_claim_ids = self._resolve_claim_refs(ev.supports_claim_ids, create_missing=True)
        ev.contradicts_claim_ids = self._resolve_claim_refs(ev.contradicts_claim_ids, create_missing=False)
        if ev.provenance.reliability == DEFAULT_RELIABILITY:
            ev.provenance.reliability = source_trust(ev.provenance.source)
        for other in self.state.evidence:
            if other.id != ev.id and self._shares_root(ev, other) and other.id not in ev.derived_from:
                ev.derived_from.append(other.id)
        self.state.evidence.append(ev)
        affected: list[str] = []
        for cid in ev.supports_claim_ids:
            claim = self.state.claim(cid)
            if claim is not None and ev.id not in claim.evidence_for:
                claim.evidence_for.append(ev.id)
                affected.append(cid)
        for cid in ev.contradicts_claim_ids:
            claim = self.state.claim(cid)
            if claim is not None and ev.id not in claim.evidence_against:
                claim.evidence_against.append(ev.id)
                affected.append(cid)
        for cid in dict.fromkeys(affected):
            claim = self.state.claim(cid)
            if claim is not None:
                self._recompute_claim(claim, touched=True)
        return ev

    def _evidence_list(self, ids: list[str]) -> list[Evidence]:
        out: list[Evidence] = []
        for eid in ids:
            ev = self.state.evidence_item(eid)
            if ev is not None:
                out.append(ev)
        return out

    @staticmethod
    def _raw_weight(ev: Evidence) -> float:
        return ev.provenance.reliability * KIND_FACTOR.get(ev.kind, 0.7) * ev.weight

    def _aggregate(self, evidence: list[Evidence]) -> _Side:
        side = _Side()
        counted_roots: set[str] = set()
        for ev in sorted(evidence, key=lambda e: (-self._raw_weight(e), e.id)):
            w = self._raw_weight(ev)
            roots = self._root_keys(ev)
            independent = {other.id for other in side.evidence if other.id in ev.independent_of or ev.id in other.independent_of}
            if roots & counted_roots and not independent:
                w *= SHARED_ROOT_DISCOUNT
            else:
                side.clusters += 1
            counted_roots |= roots
            side.weight += w
            side.max_reliability = max(side.max_reliability, ev.provenance.reliability)
            side.evidence.append(ev)
        return side

    def _sides(self, claim: Claim) -> tuple[_Side, _Side]:
        return (
            self._aggregate(self._evidence_list(claim.evidence_for)),
            self._aggregate(self._evidence_list(claim.evidence_against)),
        )

    # --- recompute -------------------------------------------------------------

    def recompute(self, claim_id: Optional[str] = None) -> None:
        if claim_id is not None:
            claim = self.state.claim(claim_id)
            if claim is not None:
                self._recompute_claim(claim)
            return
        for claim in self.state.claims:
            self._recompute_claim(claim)

    def _recompute_claim(self, claim: Claim, touched: bool = False) -> None:
        if claim.status == ClaimStatus.STALE and not touched:
            return  # stale claims stay stale until re-verified by new evidence
        side_for, side_against = self._sides(claim)
        if not side_for.evidence and not side_against.evidence:
            self._priors.setdefault(claim.id, claim.confidence)
            confidence = _clamp(claim.confidence)
        else:
            prior = self._priors.get(claim.id, 0.5)
            log_odds = _logit(prior) + LOG_ODDS_SCALE * (side_for.weight - side_against.weight)
            confidence = _clamp(_sigmoid(log_odds))
        claim.confidence = confidence
        claim.source_independence = side_for.clusters
        if side_for.evidence:
            claim.source_quality = side_for.max_reliability

        if claim.epistemic_status == EpistemicStatus.ESTABLISHED_FACT or (
            confidence >= 0.9 and side_for.clusters >= 2
        ):
            status = ClaimStatus.ESTABLISHED
        elif confidence >= 0.65:
            status = ClaimStatus.SUPPORTED
        elif confidence <= 0.15:
            status = ClaimStatus.REFUTED
        elif (
            side_for.weight >= CONTEST_WEIGHT
            and side_against.weight >= CONTEST_WEIGHT
            and abs(confidence - 0.5) < 0.25
        ):
            status = ClaimStatus.CONTESTED
        else:
            status = ClaimStatus.OPEN
        claim.status = status
        now = iso_now()
        if touched:
            claim.last_verified_at = now
        claim.updated_at = now

    # --- contradictions --------------------------------------------------------

    def declare_exclusive(self, claim_id_a: str, claim_id_b: str) -> None:
        if claim_id_a == claim_id_b:
            return
        self._exclusive.add(frozenset({claim_id_a, claim_id_b}))
        for mine, other in ((claim_id_a, claim_id_b), (claim_id_b, claim_id_a)):
            claim = self.state.claim(mine)
            marker = f"{EXCLUSIVE_PREFIX}{other}"
            if claim is not None and marker not in claim.assumptions:
                claim.assumptions.append(marker)

    @staticmethod
    def _guess_cause(for_evs: list[Evidence], against_evs: list[Evidence], default: str = "unknown") -> str:
        scopes_for = {e.scope.strip().lower() for e in for_evs if e.scope and e.scope.strip()}
        scopes_against = {e.scope.strip().lower() for e in against_evs if e.scope and e.scope.strip()}
        if scopes_for and scopes_against and scopes_for != scopes_against:
            return "scope"
        fresh_for = {e.freshness.strip() for e in for_evs if e.freshness and e.freshness.strip()}
        fresh_against = {e.freshness.strip() for e in against_evs if e.freshness and e.freshness.strip()}
        if fresh_for and fresh_against and fresh_for != fresh_against:
            return "time_period"
        return default

    def _existing_contradiction(self, claim_ids: list[str]) -> Optional[Contradiction]:
        key = tuple(sorted(claim_ids))
        for ctr in self.state.contradictions:
            if tuple(sorted(ctr.claim_ids)) == key:
                return ctr
        return None

    def _record_contradiction(self, candidate: Contradiction) -> Contradiction:
        existing = self._existing_contradiction(candidate.claim_ids)
        if existing is None:
            self.state.contradictions.append(candidate)
            return candidate
        if not existing.resolved:
            existing.severity = candidate.severity
            for eid in candidate.evidence_ids:
                if eid not in existing.evidence_ids:
                    existing.evidence_ids.append(eid)
            if existing.suspected_cause == "unknown" and candidate.suspected_cause != "unknown":
                existing.suspected_cause = candidate.suspected_cause
        return existing

    def detect_contradictions(self) -> list[Contradiction]:
        # (a) claims with substantial evidence on both sides
        for claim in self.state.claims:
            side_for, side_against = self._sides(claim)
            if side_for.weight >= CONTEST_WEIGHT and side_against.weight >= CONTEST_WEIGHT:
                severity = min(1.0, claim.decision_relevance + 0.5 * min(side_for.weight, side_against.weight))
                self._record_contradiction(
                    Contradiction(
                        claim_ids=[claim.id],
                        evidence_ids=[e.id for e in side_for.evidence + side_against.evidence],
                        description=f"Conflicting evidence for: {claim.proposition}",
                        severity=severity,
                        suspected_cause=self._guess_cause(side_for.evidence, side_against.evidence),
                    )
                )
        # (b) mutually exclusive claims that are both believed
        for pair in sorted(self._exclusive, key=lambda p: tuple(sorted(p))):
            ids = sorted(pair)
            if len(ids) != 2:
                continue
            a, b = self.state.claim(ids[0]), self.state.claim(ids[1])
            if a is None or b is None or a.confidence < 0.6 or b.confidence < 0.6:
                continue
            for_a, _ = self._sides(a)
            for_b, _ = self._sides(b)
            severity = min(1.0, (a.decision_relevance + b.decision_relevance) / 2 + 0.5 * min(for_a.weight, for_b.weight))
            self._record_contradiction(
                Contradiction(
                    claim_ids=ids,
                    evidence_ids=[e.id for e in for_a.evidence + for_b.evidence],
                    description=f"Mutually exclusive claims both believed: '{a.proposition}' vs '{b.proposition}'",
                    severity=severity,
                    suspected_cause=self._guess_cause(for_a.evidence, for_b.evidence, default="genuine"),
                )
            )
        # (c) evidence against established claims
        for claim in self.state.claims:
            if claim.status != ClaimStatus.ESTABLISHED or not claim.evidence_against:
                continue
            side_for, side_against = self._sides(claim)
            if not side_against.evidence:
                continue
            severity = min(1.0, claim.decision_relevance + 0.5 * min(side_for.weight, side_against.weight))
            self._record_contradiction(
                Contradiction(
                    claim_ids=[claim.id],
                    evidence_ids=[e.id for e in side_against.evidence],
                    description=f"Evidence contradicts established claim: {claim.proposition}",
                    severity=severity,
                    suspected_cause=self._guess_cause(side_for.evidence, side_against.evidence, default="source_error"),
                )
            )
        return sorted(self.state.unresolved_contradictions(), key=lambda c: -c.severity)

    def serious_contradictions(self, threshold: float = 0.5) -> list[Contradiction]:
        return sorted(
            (c for c in self.state.unresolved_contradictions() if c.severity >= threshold),
            key=lambda c: -c.severity,
        )

    def resolve_contradiction(self, contradiction_id: str, resolution: str, cause: str) -> None:
        for ctr in self.state.contradictions:
            if ctr.id == contradiction_id:
                ctr.resolved = True
                ctr.resolution = resolution
                if cause:
                    ctr.suspected_cause = cause
                return

    # --- staleness -------------------------------------------------------------

    def _freshness_sensitive(self, claim: Claim) -> bool:
        if claim.freshness:
            return True
        for ev in self._evidence_list(claim.evidence_for + claim.evidence_against):
            if ev.freshness:
                return True
        return False

    def mark_stale(self, max_age_days: int, now: Optional[str] = None) -> list[Claim]:
        current = _parse_ts(now) or datetime.now(timezone.utc)
        cutoff = current - timedelta(days=max_age_days)
        stale: list[Claim] = []
        for claim in self.state.claims:
            if claim.status == ClaimStatus.STALE:
                continue
            ts = _parse_ts(claim.last_verified_at) or _parse_ts(claim.created_at)
            if ts is None or ts >= cutoff or not self._freshness_sensitive(claim):
                continue
            claim.status = ClaimStatus.STALE
            claim.confidence = _clamp(claim.confidence + STALE_PULL * (0.5 - claim.confidence))
            claim.updated_at = iso_now()
            stale.append(claim)
        return stale

    # --- hypotheses ------------------------------------------------------------

    def _hypotheses_for(self, question: Optional[str]) -> list[Hypothesis]:
        if not question:
            return list(self.state.hypotheses)
        key = normalise_proposition(question)
        exact = [h for h in self.state.hypotheses if normalise_proposition(h.question) == key]
        if exact:
            return exact
        q = _tokens(question)
        return [h for h in self.state.hypotheses if _jaccard(q, _tokens(h.question)) >= FUZZY_THRESHOLD]

    def _posterior(self, h: Hypothesis) -> float:
        sup_ids = list(h.supporting_evidence)
        con_ids = list(h.contradicting_evidence)
        if h.claim_id:
            claim = self.state.claim(h.claim_id)
            if claim is not None:
                sup_ids += [e for e in claim.evidence_for if e not in sup_ids]
                con_ids += [e for e in claim.evidence_against if e not in con_ids]
        side_for = self._aggregate(self._evidence_list(sup_ids))
        side_against = self._aggregate(self._evidence_list(con_ids))
        return _clamp(_sigmoid(_logit(h.prior) + LOG_ODDS_SCALE * (side_for.weight - side_against.weight)))

    def tournament(self, question: Optional[str] = None) -> TournamentResult:
        hypotheses = self._hypotheses_for(question)
        result = TournamentResult(question=question or "")
        if not hypotheses:
            return result
        scored: list[tuple[float, Hypothesis]] = []
        for h in hypotheses:
            posterior = self._posterior(h)
            h.confidence = posterior
            if h.status != "confirmed":
                h.status = "eliminated" if posterior < 0.1 else "active"
            scored.append((h.explanatory_power * 0.4 + posterior * 0.6, h))
        scored.sort(key=lambda item: (-item[0], item[1].id))
        active = [(s, h) for s, h in scored if h.status != "eliminated"]
        if active:
            leader = active[0][1]
            if leader.status != "confirmed":
                leader.status = "leading"
            result.leading_id = leader.id
        result.ranked = [
            {"hypothesis_id": h.id, "statement": h.statement, "score": round(s, 4), "confidence": round(h.confidence, 4), "status": h.status}
            for s, h in scored
        ]
        if len(active) < 2:
            result.margin = 1.0
            if active:
                lead = active[0][1]
                result.recommended_falsification = lead.disconfirming_observations[0] if lead.disconfirming_observations else ""
            return result
        (s1, first), (s2, second) = active[0], active[1]
        result.margin = round(s1 - s2, 4)
        second_preds = set(second.unique_predictions)
        first_preds = set(first.unique_predictions)
        discriminating = [p for p in first.unique_predictions if p not in second_preds]
        discriminating += [p for p in second.unique_predictions if p not in first_preds and p not in discriminating]
        result.discriminating_predictions = discriminating
        if first.disconfirming_observations:
            result.recommended_falsification = first.disconfirming_observations[0]
        elif discriminating:
            result.recommended_falsification = f"Test discriminating prediction: {discriminating[0]}"
        return result

    # --- falsification & summaries ---------------------------------------------

    def falsification_targets(self, limit: int = 3) -> list[dict[str, Any]]:
        targets: list[dict[str, Any]] = []
        for h in self.state.hypotheses:
            if h.status != "leading":
                continue
            relevance = 0.5
            if h.claim_id:
                claim = self.state.claim(h.claim_id)
                if claim is not None:
                    relevance = claim.decision_relevance
            targets.append(
                {
                    "kind": "hypothesis",
                    "hypothesis_id": h.id,
                    "statement": h.statement,
                    "disconfirming_observations": list(h.disconfirming_observations),
                    "priority": round(relevance * h.confidence, 4),
                }
            )
        for claim in self.state.claims:
            if claim.confidence >= 0.7 and claim.decision_relevance >= 0.6:
                targets.append(
                    {
                        "kind": "claim",
                        "claim_id": claim.id,
                        "statement": claim.proposition,
                        "falsification_conditions": list(claim.falsification_conditions),
                        "priority": round(claim.decision_relevance * claim.confidence, 4),
                    }
                )
        targets.sort(key=lambda t: -t["priority"])
        return targets[:limit]

    def summary_for_workspace(self, limit: int = 8) -> list[str]:
        ordered = sorted(self.state.claims, key=lambda c: (-c.decision_relevance, -c.confidence))
        lines: list[str] = []
        for claim in ordered[:limit]:
            prop = claim.proposition if len(claim.proposition) <= 120 else claim.proposition[:117] + "..."
            lines.append(f"[{claim.status.value} {claim.confidence:.2f}, {claim.source_independence} indep] {prop}")
        return lines
