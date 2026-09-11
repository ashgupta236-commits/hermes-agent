"""World model manager operating in place on a :class:`WorldModel`.

Epistemic status is never silently upgraded: an established fact is not
overwritten by an assumption, and differing statuses are kept as versioned
properties with validity windows. All mutations are logged to a bounded
``world.history``.
"""

from __future__ import annotations

from typing import Any, Optional

from cogos.ids import iso_now
from cogos.schemas.cognition import CausalUpdateSpec, WorldUpdateSpec
from cogos.schemas.common import EpistemicStatus, Provenance
from cogos.schemas.world import CausalLink, Entity, Prediction, Property, Relation, WorldModel

HISTORY_LIMIT = 200

_STRENGTH: dict[EpistemicStatus, int] = {
    EpistemicStatus.ASSUMPTION: 0,
    EpistemicStatus.HYPOTHESIS: 1,
    EpistemicStatus.PREDICTION: 1,
    EpistemicStatus.INFERENCE: 2,
    EpistemicStatus.OBSERVATION: 3,
    EpistemicStatus.ESTABLISHED_FACT: 4,
}
_WEAK = (EpistemicStatus.ASSUMPTION, EpistemicStatus.HYPOTHESIS)
_BASE_CONFIDENCE: dict[EpistemicStatus, float] = {
    EpistemicStatus.ASSUMPTION: 0.3,
    EpistemicStatus.HYPOTHESIS: 0.4,
    EpistemicStatus.PREDICTION: 0.4,
    EpistemicStatus.INFERENCE: 0.55,
    EpistemicStatus.OBSERVATION: 0.7,
    EpistemicStatus.ESTABLISHED_FACT: 0.9,
}


def _strength(status: EpistemicStatus) -> int:
    return _STRENGTH.get(status, 0)


def _key(name: str) -> str:
    return (name or "").strip().lower()


class WorldModelManager:
    def __init__(self, world: WorldModel):
        self.world = world
        self._trim_history()

    # --- history ---------------------------------------------------------------

    def _trim_history(self) -> None:
        if len(self.world.history) > HISTORY_LIMIT:
            del self.world.history[: len(self.world.history) - HISTORY_LIMIT]

    def _log(self, event: str, **data: Any) -> None:
        entry: dict[str, Any] = {"at": iso_now(), "event": event}
        entry.update(data)
        self.world.history.append(entry)
        self._trim_history()

    # --- entities --------------------------------------------------------------

    def find_entity(self, name: str) -> Optional[Entity]:
        return self.world.entity_by_name(name)

    def upsert_entity(
        self,
        name: str,
        kind: str = "thing",
        provenance: Optional[Provenance] = None,
        epistemic_status: EpistemicStatus = EpistemicStatus.OBSERVATION,
    ) -> Entity:
        name = (name or "").strip()
        if not name:
            raise ValueError("entity name must not be empty")
        entity = self.find_entity(name)
        now = iso_now()
        if entity is None:
            entity = Entity(name=name, kind=kind or "thing", provenance=provenance, epistemic_status=epistemic_status)
            self.world.entities.append(entity)
            self._log("entity_added", entity=entity.name, kind=entity.kind, epistemic_status=epistemic_status.value)
            return entity
        changed = False
        if kind and kind != "thing" and entity.kind != kind:
            entity.kind = kind
            changed = True
        if provenance is not None and entity.provenance is None:
            entity.provenance = provenance
            changed = True
        if epistemic_status != entity.epistemic_status:
            if _strength(epistemic_status) < _strength(entity.epistemic_status):
                self._log(
                    "ignored_downgrade",
                    entity=entity.name,
                    kept=entity.epistemic_status.value,
                    ignored=epistemic_status.value,
                )
            elif provenance is not None:
                self._log(
                    "entity_status_upgraded",
                    entity=entity.name,
                    old=entity.epistemic_status.value,
                    new=epistemic_status.value,
                    source=provenance.source,
                )
                entity.epistemic_status = epistemic_status
                changed = True
            else:
                self._log(
                    "ignored_unsupported_upgrade",
                    entity=entity.name,
                    kept=entity.epistemic_status.value,
                    ignored=epistemic_status.value,
                )
        if changed:
            entity.updated_at = now
        return entity

    # --- properties ------------------------------------------------------------

    def current_property(self, entity_name: str, name: str) -> Optional[Property]:
        entity = self.find_entity(entity_name)
        if entity is None:
            return None
        key = _key(name)
        for prop in reversed(entity.properties):
            if _key(prop.name) == key and prop.valid_to is None:
                return prop
        return None

    def set_property(
        self,
        entity_name: str,
        name: str,
        value: Any,
        epistemic_status: EpistemicStatus = EpistemicStatus.OBSERVATION,
        confidence: float = 0.7,
        provenance: Optional[Provenance] = None,
        valid_from: Optional[str] = None,
    ) -> Property:
        entity = self.upsert_entity(entity_name)
        now = iso_now()
        current = self.current_property(entity.name, name)
        new_prop = Property(
            name=name,
            value=value,
            epistemic_status=epistemic_status,
            confidence=confidence,
            valid_from=valid_from or now,
            provenance=provenance,
        )
        if current is None:
            entity.properties.append(new_prop)
            entity.updated_at = now
            self._log("property_set", entity=entity.name, property=name, value=value, epistemic_status=epistemic_status.value)
            return new_prop

        if current.epistemic_status == EpistemicStatus.ESTABLISHED_FACT and epistemic_status in _WEAK:
            self._log(
                "ignored_downgrade",
                entity=entity.name,
                property=name,
                kept=current.epistemic_status.value,
                kept_value=current.value,
                ignored=epistemic_status.value,
                ignored_value=value,
            )
            return current

        if current.epistemic_status == epistemic_status:
            if current.value == value:
                current.confidence = max(current.confidence, confidence)
                if provenance is not None:
                    current.provenance = provenance
                entity.updated_at = now
                self._log("property_confirmed", entity=entity.name, property=name, value=value)
                return current
            current.valid_to = now
            entity.properties.append(new_prop)
            entity.updated_at = now
            self._log("property_changed", entity=entity.name, property=name, old=current.value, new=value)
            return new_prop

        if _strength(epistemic_status) < _strength(current.epistemic_status) and current.value == value:
            self._log(
                "ignored_weaker_duplicate",
                entity=entity.name,
                property=name,
                kept=current.epistemic_status.value,
                ignored=epistemic_status.value,
            )
            return current

        # statuses differ: keep both versions, the old one closed at now
        current.valid_to = now
        entity.properties.append(new_prop)
        entity.updated_at = now
        self._log(
            "property_versioned",
            entity=entity.name,
            property=name,
            old=current.value,
            old_status=current.epistemic_status.value,
            new=value,
            new_status=epistemic_status.value,
        )
        return new_prop

    # --- relations -------------------------------------------------------------

    def add_relation(
        self,
        source_name: str,
        target_name: str,
        kind: str,
        epistemic_status: EpistemicStatus = EpistemicStatus.OBSERVATION,
        confidence: float = 0.7,
        provenance: Optional[Provenance] = None,
    ) -> Relation:
        source = self.upsert_entity(source_name)
        target = self.upsert_entity(target_name)
        rel_kind = (kind or "related_to").strip()
        for rel in self.world.relations:
            if rel.source_id == source.id and rel.target_id == target.id and _key(rel.kind) == _key(rel_kind):
                if _strength(epistemic_status) > _strength(rel.epistemic_status) and provenance is not None:
                    self._log("relation_status_upgraded", relation=rel.id, old=rel.epistemic_status.value, new=epistemic_status.value)
                    rel.epistemic_status = epistemic_status
                    rel.confidence = confidence
                elif _strength(epistemic_status) == _strength(rel.epistemic_status):
                    rel.confidence = max(rel.confidence, confidence)
                if provenance is not None and rel.provenance is None:
                    rel.provenance = provenance
                return rel
        rel = Relation(
            source_id=source.id,
            target_id=target.id,
            kind=rel_kind,
            epistemic_status=epistemic_status,
            confidence=confidence,
            provenance=provenance,
        )
        self.world.relations.append(rel)
        self._log("relation_added", source=source.name, target=target.name, kind=rel_kind, epistemic_status=epistemic_status.value)
        return rel

    def neighbors(self, entity_name: str) -> list[Relation]:
        entity = self.find_entity(entity_name)
        if entity is None:
            return []
        return [r for r in self.world.relations if r.source_id == entity.id or r.target_id == entity.id]

    # --- causal links ----------------------------------------------------------

    @staticmethod
    def _causal_confidence(status: EpistemicStatus, evidence_count: int) -> float:
        return min(0.98, _BASE_CONFIDENCE.get(status, 0.4) + 0.05 * evidence_count)

    def add_causal(
        self,
        cause: str,
        effect: str,
        mechanism: str = "",
        strength: float = 0.5,
        epistemic_status: EpistemicStatus = EpistemicStatus.HYPOTHESIS,
        evidence_ids: Optional[list[str]] = None,
    ) -> CausalLink:
        cause = (cause or "").strip()
        effect = (effect or "").strip()
        if not cause or not effect:
            raise ValueError("cause and effect must not be empty")
        evidence_ids = [e for e in (evidence_ids or []) if e]
        for link in self.world.causal_links:
            if _key(link.cause) == _key(cause) and _key(link.effect) == _key(effect):
                for eid in evidence_ids:
                    if eid not in link.evidence_ids:
                        link.evidence_ids.append(eid)
                if mechanism and not link.mechanism:
                    link.mechanism = mechanism
                if evidence_ids:
                    link.strength = strength
                if link.epistemic_status != epistemic_status:
                    stronger = max(link.epistemic_status, epistemic_status, key=_strength)
                    weaker = min(link.epistemic_status, epistemic_status, key=_strength)
                    chosen = stronger if link.evidence_ids else weaker
                    if chosen != link.epistemic_status:
                        self._log("causal_status_changed", link=link.id, old=link.epistemic_status.value, new=chosen.value)
                    link.epistemic_status = chosen
                link.confidence = self._causal_confidence(link.epistemic_status, len(link.evidence_ids))
                self._log("causal_merged", cause=cause, effect=effect, evidence=len(link.evidence_ids))
                return link
        link = CausalLink(
            cause=cause,
            effect=effect,
            mechanism=mechanism,
            strength=strength,
            epistemic_status=epistemic_status,
            confidence=self._causal_confidence(epistemic_status, len(evidence_ids)),
            evidence_ids=evidence_ids,
        )
        self.world.causal_links.append(link)
        self._log("causal_added", cause=cause, effect=effect, epistemic_status=epistemic_status.value)
        return link

    def causal_chain(self, cause: str, depth: int = 3) -> list[list[str]]:
        """All maximal causal paths starting at ``cause`` up to ``depth`` edges (cycle-safe)."""
        start_key = _key(cause)
        start_name = next((link.cause for link in self.world.causal_links if _key(link.cause) == start_key), cause.strip())
        chains: list[list[str]] = []

        def walk(node: str, path: list[str], visited: set[str]) -> None:
            if len(path) - 1 >= depth:
                chains.append(path)
                return
            nexts = [link.effect for link in self.world.causal_links if _key(link.cause) == _key(node) and _key(link.effect) not in visited]
            if not nexts:
                if len(path) > 1:
                    chains.append(path)
                return
            for effect in nexts:
                walk(effect, path + [effect], visited | {_key(effect)})

        walk(start_name, [start_name], {start_key})
        return chains

    # --- predictions -----------------------------------------------------------

    def add_prediction(
        self,
        statement: str,
        probability: float = 0.5,
        horizon: str = "",
        based_on_claim_ids: Optional[list[str]] = None,
    ) -> Prediction:
        pred = Prediction(
            statement=statement,
            probability=probability,
            stated_probability=probability,
            horizon=horizon,
            based_on_claim_ids=list(based_on_claim_ids or []),
        )
        self.world.predictions.append(pred)
        self._log("prediction_added", prediction=pred.id, probability=probability)
        return pred

    def resolve_prediction(self, prediction_id: str, outcome: bool, note: str = "") -> Optional[Prediction]:
        for pred in self.world.predictions:
            if pred.id == prediction_id:
                # The stated probability is left exactly as recorded. A wrong prediction is
                # evidence about the model; editing it to match the outcome destroys that
                # evidence and makes the model look calibrated when it was not.
                pred.resolved = outcome
                pred.resolved_at = iso_now()
                pred.outcome = note or ("confirmed" if outcome else "refuted")
                self._log("prediction_resolved", prediction=pred.id, outcome=outcome, probability=pred.probability)
                return pred
        return None

    # --- spec application ------------------------------------------------------

    def apply(self, update: WorldUpdateSpec, provenance: Optional[Provenance] = None) -> Entity:
        entity = self.upsert_entity(update.entity, update.kind, provenance, update.epistemic_status)
        if update.property_name:
            self.set_property(
                entity.name,
                update.property_name,
                update.property_value,
                epistemic_status=update.epistemic_status,
                confidence=update.confidence,
                provenance=provenance,
            )
        if update.relation_to:
            self.add_relation(
                entity.name,
                update.relation_to,
                update.relation_kind or "related_to",
                epistemic_status=update.epistemic_status,
                confidence=update.confidence,
                provenance=provenance,
            )
        return entity

    def apply_causal(self, update: CausalUpdateSpec, evidence_ids: Optional[list[str]] = None) -> CausalLink:
        return self.add_causal(
            update.cause,
            update.effect,
            mechanism=update.mechanism,
            strength=update.strength,
            epistemic_status=update.epistemic_status,
            evidence_ids=evidence_ids,
        )

    # --- time & summaries ------------------------------------------------------

    def advance_time(self, now: Optional[str] = None) -> str:
        self.world.temporal_now = now or iso_now()
        self._log("time_advanced", now=self.world.temporal_now)
        return self.world.temporal_now

    def snapshot_summary(self, limit: int = 10) -> list[str]:
        lines: list[str] = []
        for entity in sorted(self.world.entities, key=lambda e: e.updated_at, reverse=True)[:limit]:
            tag = "" if entity.epistemic_status == EpistemicStatus.OBSERVATION else f" ({entity.epistemic_status.value})"
            props: list[str] = []
            for prop in entity.properties:
                if prop.valid_to is not None:
                    continue
                ptag = "" if prop.epistemic_status == EpistemicStatus.OBSERVATION else f" ({prop.epistemic_status.value})"
                props.append(f"{prop.name}={prop.value}{ptag}")
                if len(props) >= 5:
                    break
            line = f"{entity.name} [{entity.kind}]{tag}"
            if props:
                line += ": " + ", ".join(props)
            lines.append(line)
        return lines
