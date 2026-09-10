# Memory and consolidation

`cogos/memory/manager.py` implements a deterministic, LLM-free memory layer over `StateStore`.
Every heuristic is explainable and every record keeps its provenance. Nothing is silently
discarded: contradictions are marked on both sides, superseded records are hidden but kept.

## The nine memory classes

`MemoryClass` (`cogos/schemas/memory.py`):

| Class | Meaning | Written by the loop |
|---|---|---|
| `working` | mission-scoped scratch; never retrieved across missions; dropped when the mission ends | no writer in the loop (scoping and consolidation rules apply to records written via the API) |
| `episodic` | what happened in a cycle / mission | `_learn`: significant cycles, mission completion |
| `semantic` | facts and lessons | `_learn`: non-procedural lessons, established decision-relevant claims; consolidation promotion |
| `procedural` | how-to lessons | `_learn`: lessons containing `use `, `prefer`, `always`, `never`, `run ` |
| `relational` | relationships between entities | no writer in the loop |
| `causal` | cause -> effect links | `_learn`: world-model causal links with confidence >= 0.7 that are not hypotheses |
| `failure` | what failed and why | `_attribute`: every failed task |
| `temporal` | time-bound facts | no writer in the loop |
| `meta` | knowledge about the system's own performance | no writer in the loop |

`MemoryRecord` fields: `content`, `tags`, `mission_id`, `provenance`, `confidence`, `importance`,
`valid_from`/`valid_to`, `expires_at`, `version`, `superseded_by`, `contradicts`, `access_count`,
`last_accessed_at`, `data`, `content_hash`.

## Selective writes

`MemoryManager.write(rec)`:

* rejected (returns `None`) when `importance < memory.min_importance_to_store` (default 0.35),
  unless the class is `FAILURE` or `PROCEDURAL` (`_ALWAYS_STORE`), or when `content` is blank;
* otherwise deduplicated, given a TTL if configured, linked to what it supersedes, checked for
  contradictions, and persisted.

`remember(memory_class, content, *, tags, mission_id, confidence, importance, provenance, data,
valid_from, valid_to)` is the convenience wrapper the loop uses.

## Dedupe by content hash

`content_hash(text) = sha256(" ".join(text.lower().split()))[:24]` (case- and whitespace-insensitive).
`write` looks for a non-superseded record with the same hash **in the same class**; if found,
`_merge_duplicate` keeps the existing id and merges: `confidence = max`, `importance = max`, tags
unioned, `version += 1`, `mission_id` filled if missing, `data` keys added without overwriting,
`contradicts` links merged symmetrically, `expires_at` extended (or cleared if the incoming record
has none).

## Contradiction detection

Candidates are records sharing vocabulary (`search_memory_text`, FTS5 or `LIKE`) or tags. A
candidate conflicts with the new record when `_conflicts(a, b)` holds:

1. Jaccard similarity of the non-numeric, non-negation content terms is >= 0.6
   (`CONTRADICTION_MIN_JACCARD`), and either
2. both contain numbers, the number sets differ, and neither is a subset of the other
   (e.g. "rounding must use 2 decimals" vs "rounding must use 3 decimals"), or
3. exactly one side contains a negation (`not`, `no longer`, `never`, `cannot`, `isn't`, ...)
   and the number sets are equal.

Superseded candidates, identical-hash candidates, and `WORKING` records from other missions are
skipped. Both records get the other's id in `contradicts`; nothing is deleted or averaged.
`MemoryManager.contradictions()` lists every unresolved pair once; `python -m cogos memory
contradictions` prints them. The `adv_corrupted_memory` scenario asserts that "2 decimals" and "3
decimals" remain both visible and linked.

## Versioning and supersede

A record whose `data["supersedes"]` names an older id causes the older record's `superseded_by`
to be set and `version = max(new.version, old.version + 1)`. Superseded records are excluded from
`find_memory_by_hash`, `all_memories` (unless `include_superseded=True`), `search_memory_text` and
retrieval, but stay in the table. Consolidation (below) also supersedes near-duplicates.

## Expiry

`memory.default_ttl_days` (default `null`) sets `expires_at` on new records without one.
`retrieve` skips records whose `expires_at` is in the past; `MemoryManager.expire()` deletes them.
`expire()` is not called by the executive loop; run it from code if you want physical deletion.

## Retrieval

`retrieve(query, limit=8, mission_id=None, classes=None, now=None)`:

1. Query terms = content terms of the query (stopwords removed; falls back to raw tokens).
2. Candidates = `search_memory_text(query, limit*5)` plus any record with a tag equal to a query term.
3. Filters: superseded, class not in `classes`, `WORKING` unless `mission_id` matches, expired.
4. Score:

```
overlap  = |query_terms ∩ record_terms| / |query_terms|        # record_terms include tags
recency  = 0.5 ** (age_hours / 24 / 30)                         # 30-day half-life, hours bucketed
score    = overlap * (0.5 + 0.5*confidence) * (0.5 + 0.5*importance) * recency
score   *= 1.5 if mission_id and record.mission_id == mission_id   # MISSION_BONUS
```

   Records with `overlap == 0` are dropped; ties break on id.
5. The top `limit` records get `access_count += 1` and `last_accessed_at` updated (which also
   refreshes `updated_at`, hence the hourly age bucketing).

`MemoryConfig.max_retrieval_items` (default 12) is defined but not read; callers pass `limit`
explicitly (`_select` uses 6, `new_mission` uses 8, the `memory_search` tool defaults to 8).

## Working-memory scoping

`WORKING` records are only ever returned when the caller passes the owning `mission_id`; they are
never compared for contradictions across missions; and consolidation deletes them once their
mission reaches `complete`, `failed` or `abandoned`.

## Consolidation passes

`MemoryManager.consolidate(mission_id=None, completed_mission_ids=None)` returns
`{"dropped_working", "merged", "promoted"}`:

| Pass | Rule |
|---|---|
| (a) drop working | delete `WORKING` records whose mission is terminal (looked up in the store unless `completed_mission_ids` is given) |
| (b) merge semantic near-duplicates | sort `SEMANTIC` by confidence, importance, age; any later record with term-Jaccard >= 0.85 (`NEAR_DUPLICATE_JACCARD`) is superseded by the winner; tags, contradictions and access counts merge; `data["merged_from"]` records the losers |
| (c) promote episodic | `EPISODIC` records with `importance >= 0.8` and `access_count >= 2` are copied to `SEMANTIC` (mission-agnostic, `data["consolidated_from"]`); the source gets `data["promoted_to"]` |

`Executive._learn` runs consolidation for the current mission when
`usage.cycles % memory.consolidation_interval_cycles == 0` (default 10) and traces the counts as
`learn`. `python -m cogos memory consolidate` runs it over all missions.

## Quarantine of injected memory

`Executive._select` retrieves up to six memories for the workspace and scans each with
`governance.immune.scan_for_injection`:

```python
flags = scan_for_injection(m.content)
if flags:
    # Poisoned memory is quarantined from the workspace and reported, never followed.
    self.tracer.emit("blocked", f"memory {m.id} quarantined: injection flags {flags}", ...)
    state.notes.append(f"memory {m.id} quarantined (injection flags {flags})")
    continue
```

The record stays in the store (so the poisoning is auditable) but never reaches the model. The
scan uses the same patterns as tool output scanning (`ignore_previous`, `role_override`,
`system_tag`, `exfiltration`, `tool_command`, `destructive`, `authority_claim`,
`override_mission`, `hidden_text`, `permission_bypass`). `adv_corrupted_memory` writes a poisoned
semantic memory and asserts a `quarantined` trace and no destructive firewall verdict.

Memory lines for mission compilation (`Runtime.new_mission`) and results of the `memory_search`
tool are not passed through this scan.

## What the loop writes, and when

| Phase | Class | Content | confidence / importance |
|---|---|---|---|
| `_attribute`, task failed | `FAILURE` | `Task '<title>' failed (<kind>): <error> -> <retry|replan|abandon|escalate>` (tags `failure`, operation hint) | 0.8 / 0.6 |
| `_learn`, significant cycle (task done/failed, completion, or synthesis) | `EPISODIC` | `[<mission>] cycle N: <operation> -> <interpretation summary>` | 0.9 / 0.5 (0.8 on completion) |
| `_learn`, each `ObservationInterpretation.lessons` entry | `PROCEDURAL` or `SEMANTIC` | the lesson | 0.6 / 0.55 |
| `_learn`, every cycle | `SEMANTIC` | each `established` claim with `decision_relevance >= 0.6` (dedupe makes this idempotent) | claim confidence / 0.7 |
| `_learn`, every cycle | `CAUSAL` | `cause -> effect (mechanism)` for confident non-hypothesis links | link confidence / 0.6 |
| `_learn`, mission complete | `EPISODIC` | `Mission complete: <objective> - <conclusion>` | 0.9 / 0.9 |

`ObservationInterpretation.failure_lessons` go to `MissionState.learned_lessons` (category
`failure`) but are not written to long-term memory; `lessons` go to both.

## Reading memory

* `_select` puts `[<class> <confidence>] <content>` lines into the `relevant_memory` workspace
  section.
* `Runtime.new_mission` passes up to eight lines to the compiler as `RELEVANT MEMORY`.
* The `memory_search` tool (`retrieve_memory` operation) returns ranked records to the executive.
* CLI: `python -m cogos memory stats|search <query>|consolidate|contradictions`.
