"""MemoryManager: selective write, contradiction detection, relevance-aware retrieval,
expiry and LLM-free consolidation on top of :class:`cogos.persistence.store.StateStore`.

Everything here is deterministic and pure Python. Heuristics are deliberately
simple and explainable:

* **Selective write** – low-importance records are dropped unless they encode a
  failure or a procedure (those are always worth keeping).
* **Dedupe** – identical content (normalised hash) within the same memory class
  merges into the existing record instead of creating a duplicate.
* **Contradictions** – a new record is compared against existing records that
  share tags or vocabulary. A numeric quantity that differs on otherwise-equal
  statements, an explicit negation flip, or an explicit ``contradicts`` link
  marks *both* records; nothing is discarded so the executive can see both.
* **Versioning** – ``data["supersedes"]`` names an older record that becomes
  hidden from retrieval (``superseded_by``) but is never deleted.
* **Retrieval** – term overlap × confidence × importance × recency (30 day
  half-life) with a mission bonus; WORKING memories never leak across missions.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from cogos.config import MemoryConfig
from cogos.ids import iso_now
from cogos.persistence.store import StateStore, content_hash
from cogos.schemas.common import Provenance
from cogos.schemas.memory import MemoryClass, MemoryRecord
from cogos.schemas.mission import MissionStatus

RECENCY_HALF_LIFE_DAYS = 30.0
MISSION_BONUS = 1.5
NEAR_DUPLICATE_JACCARD = 0.85
CONTRADICTION_MIN_JACCARD = 0.6
PROMOTE_MIN_IMPORTANCE = 0.8
PROMOTE_MIN_ACCESS = 2

_ALWAYS_STORE = {MemoryClass.FAILURE, MemoryClass.PROCEDURAL}
_TERMINAL_MISSION_STATUSES = {MissionStatus.COMPLETE, MissionStatus.FAILED, MissionStatus.ABANDONED}

_STOPWORDS = frozenset(
    "a an the is are was were be been being of to in on at by for with and or but "
    "it its this that these those what which who whom how when where why does do did "
    "has have had as from into than then there here about over under".split()
)
_NEGATION_TOKENS = frozenset({"not", "no", "longer", "never", "cannot", "isnt", "arent", "wasnt", "werent", "doesnt", "dont", "didnt", "cant", "wont"})
_NEGATION_RE = re.compile(
    r"\b(?:not|no longer|never|cannot|isn'?t|aren'?t|wasn'?t|weren'?t|doesn'?t|don'?t|didn'?t|can'?t|won'?t)\b",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"(?<![\w.])[-+]?\d+(?:[.,]\d+)*(?![\w])")
_TOKEN_RE = re.compile(r"[a-z0-9]+")


# --------------------------------------------------------------------------- text helpers


def _tokens(text: str) -> list[str]:
    """Lower-cased alphanumeric tokens (apostrophes collapsed so "isn't" -> "isnt")."""
    return _TOKEN_RE.findall(text.lower().replace("'", ""))


def _content_terms(text: str) -> set[str]:
    return {t for t in _tokens(text) if t not in _STOPWORDS}


def _numbers(text: str) -> set[str]:
    out: set[str] = set()
    for m in _NUMBER_RE.finditer(text):
        raw = m.group(0).replace(",", "")
        try:
            out.add(repr(float(raw)))
        except ValueError:
            continue
    return out


def _non_numeric_terms(text: str) -> set[str]:
    return {t for t in _content_terms(text) if not t.isdigit()}


def _has_negation(text: str) -> bool:
    return _NEGATION_RE.search(text) is not None


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _record_terms(rec: MemoryRecord) -> set[str]:
    terms = _content_terms(rec.content)
    for tag in rec.tags:
        terms.add(tag.lower())
        terms.update(_content_terms(tag))
    return terms


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


def _merge_tags(base: list[str], extra: Iterable[str]) -> list[str]:
    seen = set(base)
    out = list(base)
    for t in extra:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


# --------------------------------------------------------------------------- manager


class MemoryManager:
    """Selective, versioned, contradiction-aware memory over a :class:`StateStore`."""

    def __init__(self, store: StateStore, config: Optional[MemoryConfig] = None):
        self.store = store
        self.config = config or MemoryConfig()

    # -- writing ---------------------------------------------------------------

    def write(self, rec: MemoryRecord) -> Optional[MemoryRecord]:
        """Selective write. Returns the persisted record (possibly a merged
        pre-existing one) or ``None`` when the record was rejected."""
        if rec.importance < self.config.min_importance_to_store and rec.memory_class not in _ALWAYS_STORE:
            return None
        if not rec.content.strip():
            return None

        rec.content_hash = content_hash(rec.content)

        existing = self.store.find_memory_by_hash(rec.content_hash, rec.memory_class)
        if existing is not None and existing.id != rec.id:
            return self._merge_duplicate(existing, rec)

        if rec.expires_at is None and self.config.default_ttl_days:
            base = _parse_ts(rec.created_at) or datetime.now(timezone.utc)
            rec.expires_at = (base + timedelta(days=self.config.default_ttl_days)).isoformat(timespec="milliseconds")

        supersedes = rec.data.get("supersedes") if isinstance(rec.data, dict) else None
        if isinstance(supersedes, str) and supersedes and supersedes != rec.id:
            old = self.store.get_memory(supersedes)
            if old is not None:
                old.superseded_by = rec.id
                self.store.put_memory(old)
                rec.version = max(rec.version, old.version + 1)

        conflicts = self._detect_contradictions(rec, skip_ids={supersedes} if supersedes else set())
        for other in conflicts:
            if other.id not in rec.contradicts:
                rec.contradicts.append(other.id)
            if rec.id not in other.contradicts:
                other.contradicts.append(rec.id)
                self.store.put_memory(other)
        rec.contradicts = sorted(set(rec.contradicts))

        return self.store.put_memory(rec)

    def remember(
        self,
        memory_class: MemoryClass,
        content: str,
        *,
        tags: Optional[list[str]] = None,
        mission_id: Optional[str] = None,
        confidence: float = 0.6,
        importance: float = 0.5,
        provenance: Optional[Provenance] = None,
        data: Optional[dict[str, Any]] = None,
        valid_from: Optional[str] = None,
        valid_to: Optional[str] = None,
    ) -> Optional[MemoryRecord]:
        rec = MemoryRecord(
            memory_class=memory_class,
            content=content,
            tags=list(tags or []),
            mission_id=mission_id,
            confidence=confidence,
            importance=importance,
            provenance=provenance or Provenance(source="system"),
            data=dict(data or {}),
            valid_from=valid_from,
            valid_to=valid_to,
        )
        return self.write(rec)

    def _merge_duplicate(self, existing: MemoryRecord, incoming: MemoryRecord) -> MemoryRecord:
        existing.confidence = max(existing.confidence, incoming.confidence)
        existing.importance = max(existing.importance, incoming.importance)
        existing.tags = _merge_tags(existing.tags, incoming.tags)
        existing.version += 1
        if existing.mission_id is None and incoming.mission_id:
            existing.mission_id = incoming.mission_id
        for k, v in incoming.data.items():
            existing.data.setdefault(k, v)
        for cid in incoming.contradicts:
            if cid not in existing.contradicts and cid != existing.id:
                existing.contradicts.append(cid)
                other = self.store.get_memory(cid)
                if other is not None and existing.id not in other.contradicts:
                    other.contradicts.append(existing.id)
                    self.store.put_memory(other)
        if incoming.expires_at and existing.expires_at:
            existing.expires_at = max(existing.expires_at, incoming.expires_at)
        elif incoming.expires_at is None:
            existing.expires_at = None
        return self.store.put_memory(existing)

    # -- contradictions ----------------------------------------------------------

    def _candidates_for(self, rec: MemoryRecord) -> list[MemoryRecord]:
        found: dict[str, MemoryRecord] = {}
        for cand in self.store.search_memory_text(rec.content, limit=100):
            found[cand.id] = cand
        if rec.tags:
            tagset = {t.lower() for t in rec.tags}
            for cand in self.store.all_memories():
                if tagset & {t.lower() for t in cand.tags}:
                    found[cand.id] = cand
        return [found[k] for k in sorted(found)]

    def _detect_contradictions(self, rec: MemoryRecord, skip_ids: set[str]) -> list[MemoryRecord]:
        conflicts: dict[str, MemoryRecord] = {}
        for cid in rec.contradicts:
            if cid == rec.id or cid in skip_ids:
                continue
            other = self.store.get_memory(cid)
            if other is not None:
                conflicts[other.id] = other

        for cand in self._candidates_for(rec):
            if cand.id == rec.id or cand.id in skip_ids or cand.id in conflicts:
                continue
            if cand.superseded_by or cand.content_hash == rec.content_hash:
                continue
            if cand.memory_class == MemoryClass.WORKING and cand.mission_id != rec.mission_id:
                continue
            if self._conflicts(rec.content, cand.content):
                conflicts[cand.id] = cand
        return [conflicts[k] for k in sorted(conflicts)]

    @staticmethod
    def _conflicts(a: str, b: str) -> bool:
        """Heuristic: same statement with a different numeric value, or a negation flip."""
        terms_a, terms_b = _non_numeric_terms(a) - _NEGATION_TOKENS, _non_numeric_terms(b) - _NEGATION_TOKENS
        if not terms_a or not terms_b:
            return False
        similarity = _jaccard(terms_a, terms_b)
        if similarity < CONTRADICTION_MIN_JACCARD:
            return False
        nums_a, nums_b = _numbers(a), _numbers(b)
        if nums_a and nums_b and nums_a != nums_b and not (nums_a <= nums_b or nums_b <= nums_a):
            return True
        if _has_negation(a) != _has_negation(b) and nums_a == nums_b:
            return True
        return False

    def contradictions(self) -> list[tuple[MemoryRecord, MemoryRecord]]:
        """All unresolved contradiction pairs (both records still visible), each once."""
        visible = {r.id: r for r in self.store.all_memories()}
        pairs: set[tuple[str, str]] = set()
        for rec in visible.values():
            for cid in rec.contradicts:
                if cid in visible and cid != rec.id:
                    pairs.add((min(rec.id, cid), max(rec.id, cid)))
        return [(visible[a], visible[b]) for a, b in sorted(pairs)]

    # -- retrieval -----------------------------------------------------------------

    def retrieve(
        self,
        query: str,
        limit: int = 8,
        mission_id: Optional[str] = None,
        classes: Optional[list[MemoryClass]] = None,
        now: Optional[str] = None,
    ) -> list[MemoryRecord]:
        now_iso = now or iso_now()
        now_dt = _parse_ts(now_iso) or datetime.now(timezone.utc)
        query_terms = _content_terms(query) or set(_tokens(query))
        if not query_terms:
            return []
        allowed = set(classes) if classes else None

        candidates: dict[str, MemoryRecord] = {}
        for rec in self.store.search_memory_text(query, limit=max(limit, 1) * 5):
            candidates[rec.id] = rec
        for rec in self.store.all_memories():
            if rec.id in candidates:
                continue
            if any(t.lower() in query_terms for t in rec.tags):
                candidates[rec.id] = rec

        scored: list[tuple[float, str, MemoryRecord]] = []
        for rec in candidates.values():
            if rec.superseded_by:
                continue
            if allowed is not None and rec.memory_class not in allowed:
                continue
            if rec.memory_class == MemoryClass.WORKING and (mission_id is None or rec.mission_id != mission_id):
                continue
            if rec.expires_at and rec.expires_at < now_iso:
                continue
            score = self._score(rec, query_terms, mission_id, now_dt)
            if score <= 0:
                continue
            scored.append((score, rec.id, rec))

        scored.sort(key=lambda item: (-item[0], item[1]))
        results = [rec for _, _, rec in scored[: max(limit, 0)]]
        for rec in results:
            rec.access_count += 1
            rec.last_accessed_at = now_iso
            self.store.put_memory(rec)
        return results

    @staticmethod
    def _score(rec: MemoryRecord, query_terms: set[str], mission_id: Optional[str], now_dt: datetime) -> float:
        overlap = len(query_terms & _record_terms(rec)) / len(query_terms)
        if overlap == 0:
            return 0.0
        updated = _parse_ts(rec.updated_at) or now_dt
        # Age is bucketed to whole hours: every store write refreshes ``updated_at``
        # (including access-count updates), so finer resolution would let write
        # jitter reorder otherwise-equal records. Ties fall back to id order.
        age_hours = max(0, int((now_dt - updated).total_seconds() // 3600))
        recency = 0.5 ** (age_hours / 24.0 / RECENCY_HALF_LIFE_DAYS)
        score = overlap * (0.5 + 0.5 * rec.confidence) * (0.5 + 0.5 * rec.importance) * recency
        if mission_id and rec.mission_id == mission_id:
            score *= MISSION_BONUS
        return score

    # -- maintenance --------------------------------------------------------------

    def expire(self, now: Optional[str] = None) -> int:
        now_iso = now or iso_now()
        count = 0
        for rec in self.store.all_memories(include_superseded=True):
            if rec.expires_at and rec.expires_at < now_iso:
                self.store.delete_memory(rec.id)
                count += 1
        return count

    def forget(self, memory_id: str) -> None:
        self.store.delete_memory(memory_id)

    def stats(self) -> dict[str, int]:
        out = {cls.value: 0 for cls in MemoryClass}
        superseded = 0
        for rec in self.store.all_memories(include_superseded=True):
            if rec.superseded_by:
                superseded += 1
                continue
            out[rec.memory_class.value] += 1
        out["total"] = sum(out[cls.value] for cls in MemoryClass)
        out["superseded"] = superseded
        return out

    def consolidate(self, mission_id: Optional[str] = None, completed_mission_ids: Optional[set[str]] = None) -> dict[str, int]:
        """LLM-free consolidation pass.

        (a) drop WORKING memories of completed missions, (b) merge near-duplicate
        SEMANTIC records into the higher-confidence one, (c) promote frequently
        accessed, important EPISODIC records to SEMANTIC.
        """
        counts = {"dropped_working": 0, "merged": 0, "promoted": 0}

        # (a) drop working memory of completed missions
        done: set[str] = set(completed_mission_ids or ())
        status_cache: dict[str, bool] = {}
        for rec in self.store.all_memories(memory_class=MemoryClass.WORKING, mission_id=mission_id, include_superseded=True):
            mid = rec.mission_id
            if not mid:
                continue
            if mid not in done:
                if completed_mission_ids is not None:
                    continue
                if mid not in status_cache:
                    state = self.store.load_mission(mid)
                    status_cache[mid] = state is not None and state.status in _TERMINAL_MISSION_STATUSES
                if not status_cache[mid]:
                    continue
            self.store.delete_memory(rec.id)
            counts["dropped_working"] += 1

        # (b) merge near-duplicate semantic memories
        semantic = self.store.all_memories(memory_class=MemoryClass.SEMANTIC, mission_id=mission_id)
        semantic.sort(key=lambda r: (-r.confidence, -r.importance, r.created_at, r.id))
        term_cache = {r.id: _content_terms(r.content) for r in semantic}
        absorbed: set[str] = set()
        for i, winner in enumerate(semantic):
            if winner.id in absorbed:
                continue
            changed = False
            for loser in semantic[i + 1 :]:
                if loser.id in absorbed:
                    continue
                if _jaccard(term_cache[winner.id], term_cache[loser.id]) < NEAR_DUPLICATE_JACCARD:
                    continue
                loser.superseded_by = winner.id
                self.store.put_memory(loser)
                absorbed.add(loser.id)
                winner.tags = _merge_tags(winner.tags, loser.tags)
                for cid in loser.contradicts:
                    if cid not in winner.contradicts and cid != winner.id:
                        winner.contradicts.append(cid)
                winner.access_count += loser.access_count
                winner.data.setdefault("merged_from", [])
                if isinstance(winner.data["merged_from"], list):
                    winner.data["merged_from"].append(loser.id)
                changed = True
                counts["merged"] += 1
            if changed:
                winner.version += 1
                self.store.put_memory(winner)

        # (c) promote important, repeatedly accessed episodic memories
        episodic = self.store.all_memories(memory_class=MemoryClass.EPISODIC, mission_id=mission_id)
        episodic.sort(key=lambda r: r.id)
        for rec in episodic:
            if rec.importance < PROMOTE_MIN_IMPORTANCE or rec.access_count < PROMOTE_MIN_ACCESS:
                continue
            if rec.data.get("promoted_to"):
                continue
            copy = MemoryRecord(
                memory_class=MemoryClass.SEMANTIC,
                content=rec.content,
                tags=list(rec.tags),
                mission_id=None,
                provenance=rec.provenance,
                confidence=rec.confidence,
                importance=rec.importance,
                valid_from=rec.valid_from,
                valid_to=rec.valid_to,
                data={"consolidated_from": rec.id},
            )
            promoted = self.write(copy)
            if promoted is None:
                continue
            rec.data["promoted_to"] = promoted.id
            self.store.put_memory(rec)
            counts["promoted"] += 1

        return counts
