"""Verification engine.

Nothing in cogos is "done" because a process said so: code is verified by
running tests through the tool fabric, research by structural checks on the
belief graph, data by schema/anomaly/reconciliation checks, artifacts by
hashing real files, criteria by the verification method they declare, and
the mission as a whole by a completion-integrity gate that lists exactly what
is still missing.
"""

from __future__ import annotations

import hashlib
import re
import shlex
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from cogos.ids import iso_now, new_id
from cogos.schemas.beliefs import Claim, ClaimStatus, EvidenceKind
from cogos.schemas.common import VerificationStatus
from cogos.schemas.decisions import Decision
from cogos.schemas.mission import Artifact, MissionState, SuccessCriterion, Task, TaskStatus, TestRecord
from cogos.schemas.tools import ToolCall
from cogos.schemas.verification import InputVersion, VerificationCheck, VerificationResult, cite
from cogos.tools.fabric import ToolFabric
from cogos.verification.test_outcome import InputVersionGuard, TestRunOutcome, classify_test_run

PASSED = VerificationStatus.PASSED
FAILED = VerificationStatus.FAILED
INCONCLUSIVE = VerificationStatus.INCONCLUSIVE
SKIPPED = VerificationStatus.SKIPPED

DEFAULT_TEST_COMMANDS = ["python -m pytest -q"]
INDEPENDENCE_CONFIDENCE = 0.8
MIN_INDEPENDENT_SOURCES = 2
TOKEN_OVERLAP_THRESHOLD = 0.3
ANOMALY_SIGMA = 5.0
CONTRADICTION_SEVERITY_GATE = 0.5

_STOPWORDS = frozenset(
    "the a an and or of to in on for with by from at as is are was were be been being that this these those it its "
    "than then there their they them we our you your he she his her not no nor but if so do does did done has have "
    "had having will would should could can may might must shall into over under all any each every some such via "
    "when where which who whom whose why how what about after before during between within without also more most".split()
)
_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_B32_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
    "null": (type(None),),
}




# --- helpers ---------------------------------------------------------------------


def tokens(text: str) -> set[str]:
    """Lowercase content tokens (>= 3 chars, stopwords removed)."""
    return {t for t in _TOKEN_RE.findall((text or "").lower()) if len(t) >= 3 and t not in _STOPWORDS}


def token_overlap(needle: str, haystack: str) -> float:
    """Fraction of ``needle``'s content tokens that also occur in ``haystack``."""
    a, b = tokens(needle), tokens(haystack)
    if not a:
        return 0.0
    return len(a & b) / len(a)


def _aggregate(checks: list[VerificationCheck]) -> VerificationStatus:
    statuses = {c.status for c in checks}
    if FAILED in statuses:
        return FAILED
    if INCONCLUSIVE in statuses:
        return INCONCLUSIVE
    if PASSED in statuses:
        return PASSED
    return INCONCLUSIVE


def _counts(checks: list[VerificationCheck]) -> str:
    n = {s: 0 for s in VerificationStatus}
    for c in checks:
        n[c.status] += 1
    return f"{n[PASSED]} passed, {n[FAILED]} failed, {n[INCONCLUSIVE]} inconclusive, {n[SKIPPED]} skipped"


def _id_timestamp_ms(identifier: str) -> Optional[int]:
    """Recover the millisecond timestamp embedded in a ``cogos.ids.new_id`` identifier."""
    try:
        _, body = identifier.rsplit("_", 1)
    except ValueError:
        return None
    if len(body) < 9:
        return None
    value = 0
    for ch in body[:9]:
        idx = _B32_ALPHABET.find(ch)
        if idx < 0:
            return None
        value = (value << 5) | idx
    return value


def _iso_to_ms(stamp: Optional[str]) -> Optional[int]:
    if not stamp:
        return None
    try:
        return int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def file_sha256(path: Path) -> Optional[str]:
    """sha256 of a file's bytes, or None when it cannot be read."""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def artifact_integrity(artifact: Artifact) -> tuple[bool, str]:
    """Re-check a *previously verified* artifact against the filesystem, now.

    `verified` is a claim about the past. This answers the question the completion gate
    actually needs answered: are those exact bytes still there?
    """
    if not artifact.verified:
        return False, "not verified"
    if not artifact.verified_hash:
        return False, "verified without a recorded content hash"
    if not artifact.path:
        return False, "declares no path"
    path = Path(artifact.path).expanduser()
    if not path.exists():
        return False, f"{path} no longer exists"
    if not path.is_file():
        return False, f"{path} is no longer a regular file"
    current = file_sha256(path)
    if current is None:
        return False, f"{path} could not be read"
    if current != artifact.verified_hash:
        return False, f"content changed since verification (sha256 {current[:12]}… != {artifact.verified_hash[:12]}…)"
    return True, f"sha256 {artifact.verified_hash[:12]}… unchanged"


def receipt_inputs_intact(receipt: VerificationResult) -> tuple[bool, str]:
    """Do the inputs this receipt was produced against still hold, right now?

    A receipt describes bytes. Once those bytes move it still records what it saw — history is
    kept — but it no longer describes what is there, so it cannot close a criterion. A receipt
    that recorded no input versions has nothing to contradict it and is reported intact; whether
    that is *sufficient* is the gate's separate question.
    """
    for iv in receipt.input_versions:
        if iv.changed_during_verification:
            return False, f"inputs changed while it ran ({Path(iv.path).name})"
        if not iv.content_hash:
            continue
        current = file_sha256(Path(iv.path)) if Path(iv.path).is_file() else None
        if current != iv.content_hash:
            return False, (
                f"{Path(iv.path).name} is now "
                + (f"sha256 {current[:12]}…" if current else "missing")
                + f", not the {iv.content_hash[:12]}… it was verified against"
            )
    return True, ""


def _type_ok(value: Any, expected: Any) -> bool:
    names = expected if isinstance(expected, list) else [expected]
    for name in names:
        allowed = _JSON_TYPES.get(str(name))
        if allowed is None:
            return True
        if isinstance(value, bool) and name in ("number", "integer"):
            continue
        if isinstance(value, allowed):
            if name == "integer" and isinstance(value, float) and not float(value).is_integer():
                continue
            return True
    return False


# --- engine ----------------------------------------------------------------------


class VerificationEngine:
    def __init__(self, fabric: Optional[ToolFabric], state: MissionState):
        self.fabric = fabric
        self.state = state
        self.results: list[VerificationResult] = []

    #: Soft cap on *unreferenced* records. Referenced receipts are never evicted: a gate that
    #: cannot resolve the receipt a criterion cites cannot tell a forged citation from a
    #: truncated store, so the store must be durable for anything still pointed at.
    MAX_UNREFERENCED_VERIFICATIONS = 500

    def record(self, result: VerificationResult) -> VerificationResult:
        """Persist a verification record into mission state so gates can resolve it later."""
        self.results.append(result)
        if self.state is not None:
            if not any(v.id == result.id for v in self.state.verifications):
                self.state.verifications.append(result)
            self.prune()
        return result

    def prune(self) -> int:
        """Drop the oldest records that nothing references. Returns how many were dropped."""
        records = self.state.verifications
        referenced = self.state.referenced_verification_ids()
        droppable = [i for i, v in enumerate(records) if v.id not in referenced]
        excess = len(droppable) - self.MAX_UNREFERENCED_VERIFICATIONS
        if excess <= 0:
            return 0
        drop = set(droppable[:excess])
        self.state.verifications = [v for i, v in enumerate(records) if i not in drop]
        return len(drop)

    # -- code -----------------------------------------------------------------------

    def _relevant_inputs(self, cwd: Optional[str], input_paths: Optional[list[str]]) -> list[tuple[Path, Optional[str]]]:
        """The input versions a code receipt is about: the mission's own registered candidates.

        Bounded deliberately to the verification contract (declared paths) plus the artifact
        ledger. This is version binding, not a general dependency graph: a receipt should name the
        implementation and test files whose bytes determine what it proves, and nothing else.
        """
        out: list[tuple[Path, Optional[str]]] = []
        seen: set[str] = set()
        for raw in input_paths or []:
            p = Path(raw)
            if not p.is_absolute() and cwd:
                p = Path(cwd) / p
            if str(p) not in seen:
                seen.add(str(p))
                out.append((p, None))
        for art in self.state.artifacts:
            if not art.path:
                continue
            p = Path(art.path)
            if str(p) in seen:
                continue
            seen.add(str(p))
            out.append((p, art.id))
        return out

    def verify_code(
        self,
        commands: Optional[list[str]] = None,
        cwd: Optional[str] = None,
        task_id: Optional[str] = None,
        *,
        expect_zero_tests: bool = False,
        criterion_ids: Optional[list[str]] = None,
        input_paths: Optional[list[str]] = None,
    ) -> VerificationResult:
        """Run test commands and record what they actually demonstrated.

        ``expect_zero_tests`` comes from the verification contract as declared *before* the run.
        It is the only way a zero-execution run can pass, and it cannot be supplied afterwards to
        reinterpret an empty result.
        """
        commands = list(commands) if commands else list(DEFAULT_TEST_COMMANDS)
        criterion_ids = list(criterion_ids or [])
        if not criterion_ids and task_id:
            # Make the record self-describing: scope comes from what the task declared it was for,
            # which was fixed before the run and cannot be attached afterwards to fit the outcome.
            producing = self.state.task(task_id)
            if producing is not None:
                criterion_ids = list(producing.addresses_criterion_ids or [])
        checks: list[VerificationCheck] = []
        test_totals = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
        if self.fabric is None:
            for cmd in commands:
                checks.append(VerificationCheck(name=cmd, status=INCONCLUSIVE, detail="no tool fabric", authoritative=True))
                self.state.tests.append(TestRecord(name=cmd, command=cmd, status=SKIPPED, summary="no tool fabric", ran_at=iso_now(), task_id=task_id, criterion_ids=criterion_ids, cwd=str(cwd or "")))
            return self.record(
                VerificationResult(target_type="code", target_id=task_id or "code", status=INCONCLUSIVE, summary="no tool fabric: tests not run", checks=checks, produced_by_task_id=task_id)
            )

        relevant = self._relevant_inputs(cwd, input_paths)
        guard = InputVersionGuard([p for p, _ in relevant])
        before = guard.snapshot()
        action_ids: list[str] = []

        for cmd in commands:
            shell_cmd = f"cd {shlex.quote(str(cwd))} && {cmd}" if cwd else cmd
            res = self.fabric.execute(ToolCall(tool="run_tests", arguments={"command": shell_cmd}, task_id=task_id, purpose="verification"))
            action_ids.append(res.call_id)
            for key, value in (res.data.get("counts") or {}).items():
                if key in test_totals:
                    test_totals[key] += int(value)
            if res.error_kind in ("denied", "requires_human", "unavailable"):
                # The check could not be run at all. That is an absence of evidence, not a verdict.
                outcome = TestRunOutcome(status=INCONCLUSIVE, reason=res.error or "verification tool unavailable", exit_code=None, command=cmd)
            else:
                outcome = classify_test_run(
                    exit_code=res.data.get("exit_code"),
                    counts=res.data.get("counts") or {},
                    output=res.output or "",
                    command=shell_cmd,
                    expect_zero=expect_zero_tests,
                )
            detail = f"{outcome.reason}: {res.data.get('summary') or res.error or ''}".strip(": ")
            checks.append(VerificationCheck(name=cmd, status=outcome.status, detail=detail, authoritative=True))
            self.state.tests.append(
                TestRecord(
                    name=cmd,
                    command=shell_cmd,
                    status=outcome.status,
                    summary=detail,
                    ran_at=iso_now(),
                    task_id=task_id,
                    criterion_ids=criterion_ids,
                    cwd=str(cwd or ""),
                    framework=outcome.framework,
                    exit_code=outcome.exit_code,
                    counts={"passed": outcome.passed, "failed": outcome.failed, "error": outcome.errors, "skipped": outcome.skipped},
                    executed=outcome.executed,
                    outcome_reason=outcome.reason,
                    expected_zero=outcome.expected_zero,
                )
            )

        moved = set(guard.changed_since(before))
        after = guard.snapshot()
        input_versions = [
            InputVersion(path=str(p), content_hash=after.get(str(p), ""), artifact_id=aid, changed_during_verification=str(p) in moved)
            for p, aid in relevant
        ]
        status = _aggregate(checks)
        if moved:
            # The bytes moved underneath the run, so no single coherent version was observed.
            status = INCONCLUSIVE
            checks.append(VerificationCheck(name="input_stability", status=INCONCLUSIVE, detail="inputs changed during verification: " + ", ".join(sorted(moved)[:3]), authoritative=True))
        n_pass = sum(1 for c in checks if c.status == PASSED)
        n_fail = sum(1 for c in checks if c.status == FAILED)
        n_inc = len(checks) - n_pass - n_fail
        summary = (
            f"{n_pass}/{len(checks)} commands passed, {n_fail} failed, {n_inc} inconclusive; "
            f"tests: {test_totals['passed']} passed, {test_totals['failed']} failed, {test_totals['error']} errors, "
            f"{test_totals['skipped']} skipped"
        )
        return self.record(
            VerificationResult(
                target_type="code",
                target_id=task_id or "code",
                status=status,
                summary=summary,
                checks=checks,
                input_versions=input_versions,
                produced_by_task_id=task_id,
                produced_by_action_ids=action_ids,
            )
        )

    # -- research -------------------------------------------------------------------

    def _unresolved_contradictions_for(self, claim_id: str):
        return [c for c in self.state.contradictions if not c.resolved and claim_id in c.claim_ids]

    def _check_claim(self, claim: Claim, checks: list[VerificationCheck], evidence_ids: list[str]) -> None:
        cid = claim.id
        evidence = [e for e in (self.state.evidence_item(eid) for eid in claim.evidence_for) if e is not None]
        evidence_ids.extend(e.id for e in evidence)
        missing = [eid for eid in claim.evidence_for if self.state.evidence_item(eid) is None]

        if evidence:
            checks.append(VerificationCheck(name=f"{cid}:has_evidence", status=PASSED, detail=f"{len(evidence)} supporting evidence item(s)" + (f"; {len(missing)} referenced id(s) missing" if missing else "")))
        else:
            checks.append(VerificationCheck(name=f"{cid}:has_evidence", status=FAILED, detail="claim has no resolvable supporting evidence"))

        roots: set[str] = set()
        for e in evidence:
            roots |= e.root_sources()
        if claim.confidence >= INDEPENDENCE_CONFIDENCE:
            if len(roots) >= MIN_INDEPENDENT_SOURCES:
                checks.append(VerificationCheck(name=f"{cid}:independence", status=PASSED, detail=f"{len(roots)} independent root sources"))
            else:
                checks.append(VerificationCheck(name=f"{cid}:independence", status=FAILED, detail=f"confidence {claim.confidence:.2f} rests on {len(roots)} independent root source(s); need >= {MIN_INDEPENDENT_SOURCES}"))
        else:
            checks.append(VerificationCheck(name=f"{cid}:independence", status=SKIPPED, detail=f"{len(roots)} root source(s); independence only required at confidence >= {INDEPENDENCE_CONFIDENCE}"))

        if claim.status == ClaimStatus.ESTABLISHED:
            if any(e.kind == EvidenceKind.PRIMARY for e in evidence):
                checks.append(VerificationCheck(name=f"{cid}:primary_evidence", status=PASSED, detail="established claim backed by primary evidence"))
            else:
                checks.append(VerificationCheck(name=f"{cid}:primary_evidence", status=INCONCLUSIVE, detail="established claim has no primary evidence"))
        else:
            checks.append(VerificationCheck(name=f"{cid}:primary_evidence", status=SKIPPED, detail="only required for established claims"))

        if any(e.freshness for e in evidence):
            if claim.freshness:
                checks.append(VerificationCheck(name=f"{cid}:freshness", status=PASSED, detail=f"claim freshness {claim.freshness}"))
            else:
                checks.append(VerificationCheck(name=f"{cid}:freshness", status=INCONCLUSIVE, detail="evidence carries freshness but the claim records none"))
        else:
            checks.append(VerificationCheck(name=f"{cid}:freshness", status=SKIPPED, detail="no dated evidence"))

        narrow = []
        for e in evidence:
            if e.supports_proposition:
                overlap = token_overlap(e.supports_proposition, claim.proposition)
                if overlap < TOKEN_OVERLAP_THRESHOLD:
                    narrow.append(f"{e.id} ({overlap:.0%} overlap)")
        if narrow:
            checks.append(VerificationCheck(name=f"{cid}:consistency", status=INCONCLUSIVE, detail="evidence supports narrower proposition: " + ", ".join(narrow)))
        else:
            checks.append(VerificationCheck(name=f"{cid}:consistency", status=PASSED, detail="evidence propositions consistent with claim"))

        open_contradictions = self._unresolved_contradictions_for(cid)
        if open_contradictions:
            checks.append(VerificationCheck(name=f"{cid}:contradictions", status=FAILED, detail="unresolved contradiction(s): " + ", ".join(c.id for c in open_contradictions)))
        else:
            checks.append(VerificationCheck(name=f"{cid}:contradictions", status=PASSED, detail="no unresolved contradictions"))

    def verify_research(self, claim_ids: Optional[list[str]] = None, task_id: Optional[str] = None) -> VerificationResult:
        checks: list[VerificationCheck] = []
        evidence_ids: list[str] = []
        if claim_ids is None:
            claims: list[Optional[Claim]] = list(self.state.claims)
            ids = [c.id for c in self.state.claims]
        else:
            ids = list(claim_ids)
            claims = [self.state.claim(cid) for cid in ids]
        for cid, claim in zip(ids, claims):
            if claim is None:
                checks.append(VerificationCheck(name=f"{cid}:exists", status=FAILED, detail="claim not found in mission state"))
                continue
            self._check_claim(claim, checks, evidence_ids)
        if not checks:
            checks.append(VerificationCheck(name="claims", status=INCONCLUSIVE, detail="no claims to verify"))
        status = _aggregate(checks)
        failed = [c for c in checks if c.status == FAILED]
        summary = f"{len(ids)} claim(s): {_counts(checks)}"
        if failed:
            summary += "; failing: " + "; ".join(f"{c.name} ({c.detail})" for c in failed[:5])
        return self.record(
            VerificationResult(target_type="claim", target_id=task_id or (",".join(ids) if ids else "claims"), status=status, summary=summary, checks=checks, evidence_ids=sorted(set(evidence_ids)))
        )

    # -- data -----------------------------------------------------------------------

    def verify_data(self, records: list[dict[str, Any]], schema: dict[str, Any], reconciliation: Optional[dict[str, Any]] = None) -> VerificationResult:
        checks: list[VerificationCheck] = []
        required = list(schema.get("required") or [])
        properties: dict[str, Any] = dict(schema.get("properties") or {})
        violations: list[str] = []
        for i, rec in enumerate(records):
            if not isinstance(rec, dict):
                violations.append(f"record {i}: not an object")
                continue
            for key in required:
                if key not in rec:
                    violations.append(f"record {i}: missing required '{key}'")
            for key, spec in properties.items():
                if key in rec and isinstance(spec, dict) and "type" in spec and not _type_ok(rec[key], spec["type"]):
                    violations.append(f"record {i}: '{key}' expected {spec['type']}, got {type(rec[key]).__name__}")
        if violations:
            checks.append(VerificationCheck(name="schema", status=FAILED, detail=f"{len(violations)} violation(s): " + "; ".join(violations[:5])))
        else:
            checks.append(VerificationCheck(name="schema", status=PASSED, detail=f"{len(records)} record(s) conform"))

        numeric_fields = [k for k, s in properties.items() if isinstance(s, dict) and s.get("type") in ("number", "integer")]
        if not numeric_fields:
            candidates: set[str] = set()
            for rec in records:
                if isinstance(rec, dict):
                    candidates |= {k for k, v in rec.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
            numeric_fields = sorted(candidates)
        anomalies: list[str] = []
        for field in numeric_fields:
            values = [(i, float(r[field])) for i, r in enumerate(records) if isinstance(r, dict) and isinstance(r.get(field), (int, float)) and not isinstance(r.get(field), bool)]
            if len(values) < 3:
                continue
            xs = [v for _, v in values]
            mean = statistics.fmean(xs)
            std = statistics.pstdev(xs)
            if std <= 0:
                continue
            for i, v in values:
                if abs(v - mean) > ANOMALY_SIGMA * std:
                    anomalies.append(f"record {i}: {field}={v:g} is {abs(v - mean) / std:.1f} std from mean {mean:g}")
        if anomalies:
            checks.append(VerificationCheck(name="anomalies", status=INCONCLUSIVE, detail="; ".join(anomalies[:5])))
        else:
            checks.append(VerificationCheck(name="anomalies", status=PASSED, detail=f"no values beyond {ANOMALY_SIGMA:g} std in {len(numeric_fields)} numeric field(s)"))

        if reconciliation:
            field = str(reconciliation.get("field", ""))
            tolerance = float(reconciliation.get("tolerance", 0.0))
            rel_tol = float(reconciliation.get("relative_tolerance", 0.0))
            details: list[str] = []
            ok = True
            if "expected_total" in reconciliation:
                total = sum(float(r.get(field, 0) or 0) for r in records if isinstance(r, dict) and isinstance(r.get(field), (int, float)))
                expected = float(reconciliation["expected_total"])
                allowed = max(tolerance, rel_tol * abs(expected))
                diff = abs(total - expected)
                if diff > allowed:
                    ok = False
                details.append(f"sum({field})={total:g} vs expected {expected:g} (diff {diff:g}, tolerance {allowed:g})")
            if "expected_count" in reconciliation:
                expected_count = int(reconciliation["expected_count"])
                if len(records) != expected_count:
                    ok = False
                details.append(f"count={len(records)} vs expected {expected_count}")
            if not details:
                checks.append(VerificationCheck(name="reconciliation", status=INCONCLUSIVE, detail="reconciliation spec has no expected_total or expected_count"))
            else:
                checks.append(VerificationCheck(name="reconciliation", status=PASSED if ok else FAILED, detail="; ".join(details)))

        status = _aggregate(checks)
        summary = f"{len(records)} record(s): {_counts(checks)}"
        failed = [c for c in checks if c.status == FAILED]
        if failed:
            summary += "; failing: " + "; ".join(f"{c.name} ({c.detail})" for c in failed)
        return self.record(VerificationResult(target_type="data", target_id=str(schema.get("title") or "records"), status=status, summary=summary, checks=checks))

    # -- artifacts ------------------------------------------------------------------

    def verify_artifact(self, artifact: Artifact) -> VerificationResult:
        checks: list[VerificationCheck] = []
        path = Path(artifact.path).expanduser() if artifact.path else None
        if path is None:
            checks.append(VerificationCheck(name="path", status=FAILED, detail="artifact declares no path", authoritative=True))
        elif not path.exists():
            checks.append(VerificationCheck(name="path", status=FAILED, detail=f"{path} does not exist", authoritative=True))
        elif not path.is_file():
            checks.append(VerificationCheck(name="path", status=FAILED, detail=f"{path} is not a regular file", authoritative=True))
        else:
            checks.append(VerificationCheck(name="path", status=PASSED, detail=str(path)))
            size = path.stat().st_size
            if size == 0:
                checks.append(VerificationCheck(name="non_empty", status=FAILED, detail="file is empty", authoritative=True))
            else:
                checks.append(VerificationCheck(name="non_empty", status=PASSED, detail=f"{size} bytes"))
            content_hash = file_sha256(path) or ""
            # The comparison that matters is against the hash this artifact last *passed* on.
            # A re-check of changed bytes is a real check of the bytes that are there now, so it
            # passes and records the change; it is the completion gate that refuses to accept
            # the stale receipt in between.
            previous = artifact.verified_hash
            artifact.content_hash = content_hash
            if previous and previous != content_hash:
                checks.append(VerificationCheck(name="hash", status=PASSED, detail=f"sha256 {content_hash} (changed since it was verified at {previous[:12]}…; re-checked)"))
            else:
                checks.append(VerificationCheck(name="hash", status=PASSED, detail=f"sha256 {content_hash}"))
        status = _aggregate(checks)
        artifact.verified = status == PASSED
        # The hash the artifact was verified *at* is what later integrity checks compare against,
        # so a file that is deleted or edited after this point stops counting as verified.
        artifact.verified_hash = artifact.content_hash if artifact.verified else None
        artifact.verified_at = iso_now() if artifact.verified else None
        stored = next((a for a in self.state.artifacts if a.id == artifact.id), None)
        if stored is not None and stored is not artifact:
            stored.content_hash = artifact.content_hash
            stored.verified = artifact.verified
            stored.verified_hash = artifact.verified_hash
            stored.verified_at = artifact.verified_at
        summary = f"artifact '{artifact.name}': {_counts(checks)}"
        return self.record(VerificationResult(target_type="artifact", target_id=artifact.id, status=status, summary=summary, checks=checks))

    # -- criteria -------------------------------------------------------------------

    def _supporting_versions(self, criterion: SuccessCriterion) -> tuple[list[InputVersion], list[str]]:
        """Input versions and action ids of the evidence this criterion actually rests on.

        A criterion receipt is a summary of other receipts. It has to inherit their version
        identity, or the chain criterion -> receipt -> evidence -> action breaks at the first hop
        and the gate re-checks nothing.
        """
        paths: dict[str, Optional[str]] = {}
        actions: list[str] = []
        task_ids = {t.task_id for t in self.state.tests if t.task_id and self._test_addresses(t, criterion.id)}
        for res in self.state.verifications:
            relevant = res.target_type in ("code", "task") and (
                (res.produced_by_task_id and res.produced_by_task_id in task_ids) or res.target_id in task_ids
            )
            # A supporting receipt whose own inputs have since moved is history, not support. It
            # contributes nothing here, so a fresh conclusion is not born stale by inheriting the
            # hashes of a superseded run.
            if not relevant or not receipt_inputs_intact(res)[0]:
                continue
            for iv in res.input_versions:
                paths.setdefault(iv.path, iv.artifact_id)
            actions.extend(a for a in res.produced_by_action_ids if a not in actions)
        # Verified artifacts are re-checked independently by artifact_integrity, but binding them
        # here keeps the criterion's own receipt self-contained for traversal.
        for art in self.state.artifacts:
            if art.verified and art.verified_hash and art.path:
                paths.setdefault(art.path, art.id)
        # Record the bytes as they are at the moment this conclusion is drawn. That is what the
        # completion gate later re-reads, so "still true?" is answerable against the conclusion.
        versions = [
            InputVersion(path=p, content_hash=(file_sha256(Path(p)) or "") if Path(p).is_file() else "", artifact_id=aid)
            for p, aid in paths.items()
        ]
        return versions, actions

    @staticmethod
    def _test_executed_something(record: TestRecord) -> bool:
        """Did this record observe a required test actually run?

        Records the engine produces carry structured counts, so ``executed`` is authoritative:
        a zero-execution run (all skipped, or an authorized expected-zero) is a legitimate
        outcome but never proof that the required tests ran. Records with no counts at all
        predate structured capture; for those the recorded status is all there is.
        """
        if not record.counts:
            return True
        return record.executed > 0

    def _test_addresses(self, record: TestRecord, criterion_id: str) -> bool:
        """Is this test record offered as evidence for *this* criterion?

        Either the record names the criterion directly, or the task that produced it declared the
        criterion in ``addresses_criterion_ids``. Both are machine-readable links recorded before
        the outcome was known, so neither can be attached after the fact to fit a result.
        """
        if criterion_id in (record.criterion_ids or []):
            return True
        if record.task_id:
            task = self.state.task(record.task_id)
            if task is not None and criterion_id in (task.addresses_criterion_ids or []):
                return True
        return False

    def verify_criterion(self, criterion: SuccessCriterion, evidence_ok: Optional[bool] = None) -> VerificationResult:
        method = (criterion.verification_method or "").lower()
        checks: list[VerificationCheck] = []
        evidence_ids: list[str] = []
        created_ms = _id_timestamp_ms(criterion.id) or 0

        if re.search(r"\b(tests?|pytest|unit\s*tests?)\b", method):
            # Scope binds the proof to the claim. A passing suite demonstrates something about the
            # criteria it was *run for*; without that link any unrelated green run would satisfy
            # any criterion whose method happens to mention tests.
            fresh = [
                t
                for t in self.state.tests
                if t.status == PASSED
                and not t.expected_failure
                and (_iso_to_ms(t.ran_at) or -1) >= created_ms
                and self._test_addresses(t, criterion.id)
                and self._test_executed_something(t)
            ]
            unbound = [
                t
                for t in self.state.tests
                if t.status == PASSED and not t.expected_failure and (_iso_to_ms(t.ran_at) or -1) >= created_ms and not self._test_addresses(t, criterion.id)
            ]
            if fresh:
                checks.append(VerificationCheck(name="tests", status=PASSED, detail=f"{len(fresh)} passed test record(s) bound to this criterion and newer than it: " + ", ".join(t.name for t in fresh[:3])))
            elif unbound:
                checks.append(VerificationCheck(name="tests", status=FAILED, detail=f"{len(unbound)} passing test record(s) exist but none is scoped to this criterion; an unrelated suite is not proof of it"))
            else:
                checks.append(VerificationCheck(name="tests", status=FAILED, detail="no passed (non-reproduction) test record newer than the criterion"))

        if "artifact" in method:
            matches = [a for a in self.state.artifacts if a.verified and token_overlap(criterion.description, f"{a.name} {a.summary}") >= TOKEN_OVERLAP_THRESHOLD]
            if matches:
                checks.append(VerificationCheck(name="artifact", status=PASSED, detail="verified artifact(s): " + ", ".join(a.name for a in matches[:3])))
            else:
                checks.append(VerificationCheck(name="artifact", status=FAILED, detail="no verified artifact mentions the criterion"))

        if evidence_ok is None and re.search(r"\b(evidence|sources?)\b", method):
            related = [c for c in self.state.claims if token_overlap(criterion.description, c.proposition) >= TOKEN_OVERLAP_THRESHOLD]
            good = [c for c in related if c.status in (ClaimStatus.SUPPORTED, ClaimStatus.ESTABLISHED) and not self._unresolved_contradictions_for(c.id)]
            if good:
                for c in good:
                    evidence_ids.extend(c.evidence_for)
                checks.append(VerificationCheck(name="evidence", status=PASSED, detail="supported claim(s): " + ", ".join(c.id for c in good[:3])))
            elif related:
                checks.append(VerificationCheck(name="evidence", status=FAILED, detail=f"{len(related)} related claim(s) but none supported/established without open contradictions"))
            else:
                checks.append(VerificationCheck(name="evidence", status=FAILED, detail="no claim relates to the criterion"))

        if evidence_ok is not None:
            related = [c for c in self.state.claims if token_overlap(criterion.description, c.proposition) >= TOKEN_OVERLAP_THRESHOLD]
            for c in related:
                if c.status in (ClaimStatus.SUPPORTED, ClaimStatus.ESTABLISHED):
                    evidence_ids.extend(c.evidence_for)
            checks.append(VerificationCheck(name="explicit_evidence", status=PASSED if evidence_ok else FAILED, detail="deterministic runtime judgement of the evidence state supplied by the executive"))
        if not checks:
            checks.append(VerificationCheck(name="method", status=INCONCLUSIVE, detail=f"verification method '{criterion.verification_method}' is not machine-checkable and no evidence_ok was supplied"))

        status = _aggregate(checks)
        criterion.satisfied = status == PASSED
        # Carry the supporting evidence's version identity onto the criterion receipt. Without
        # this the receipt names no inputs, so the completion gate has nothing to re-check and a
        # post-verification swap of the implementation goes unnoticed.
        supporting, actions = self._supporting_versions(criterion)
        result = VerificationResult(
            target_type="criterion",
            target_id=criterion.id,
            status=status,
            summary=f"criterion '{criterion.description}': {_counts(checks)}",
            checks=checks,
            evidence_ids=sorted(set(evidence_ids)),
            input_versions=supporting,
            produced_by_action_ids=actions,
        )
        # Only a passing record is citable evidence. A failed or inconclusive attempt is still
        # persisted (see record()), but citing it would turn the completion gate into a
        # has-this-been-attempted check.
        cite(criterion.verification_ids, result, criterion.id, target_type="criterion")
        return self.record(result)

    # -- decisions ------------------------------------------------------------------

    def verify_decision(self, decision: Decision, simulation_result: Any = None) -> VerificationResult:
        checks: list[VerificationCheck] = []
        if decision.concise_rationale.strip():
            checks.append(VerificationCheck(name="rationale", status=PASSED, detail="rationale recorded"))
        else:
            checks.append(VerificationCheck(name="rationale", status=FAILED, detail="rationale is empty"))

        known = [eid for eid in decision.decisive_evidence if self.state.evidence_item(eid) is not None]
        unknown = [eid for eid in decision.decisive_evidence if self.state.evidence_item(eid) is None]
        if known:
            checks.append(VerificationCheck(name="decisive_evidence", status=PASSED, detail=f"{len(known)} evidence id(s) resolved" + (f"; unknown: {', '.join(unknown)}" if unknown else "")))
        elif unknown:
            checks.append(VerificationCheck(name="decisive_evidence", status=FAILED, detail="referenced evidence not in mission state: " + ", ".join(unknown)))
        else:
            checks.append(VerificationCheck(name="decisive_evidence", status=FAILED, detail="no decisive evidence cited"))

        if decision.assumptions:
            checks.append(VerificationCheck(name="assumptions", status=PASSED, detail=f"{len(decision.assumptions)} load-bearing assumption(s) listed"))
        else:
            checks.append(VerificationCheck(name="assumptions", status=INCONCLUSIVE, detail="no load-bearing assumptions listed"))

        if simulation_result is not None:
            best = str(getattr(simulation_result, "best_option", "") or "")
            robust = bool(getattr(simulation_result, "robust_best", False))
            if decision.selected_option == best:
                checks.append(VerificationCheck(name="simulation_agreement", status=PASSED, detail=f"selected option matches simulation best '{best}'"))
            elif best and best.lower() in decision.concise_rationale.lower():
                checks.append(VerificationCheck(name="simulation_agreement", status=PASSED, detail=f"deviates from simulation best '{best}' but the rationale addresses it"))
            else:
                checks.append(VerificationCheck(name="simulation_agreement", status=FAILED, detail=f"selected '{decision.selected_option}' but simulation prefers '{best}' and the rationale does not say why"))
            if robust:
                checks.append(VerificationCheck(name="robustness", status=PASSED, detail="simulation best is robust across assumption extremes"))
            else:
                checks.append(VerificationCheck(name="robustness", status=INCONCLUSIVE, detail="sensitive to assumptions"))

        status = _aggregate(checks)
        return self.record(
            VerificationResult(target_type="decision", target_id=decision.decision_id, status=status, summary=f"decision '{decision.selected_option}': {_counts(checks)}", checks=checks, evidence_ids=known)
        )

    # -- tasks ----------------------------------------------------------------------

    def verify_task(self, task: Task) -> VerificationResult:
        params = task.parameters or {}
        commands: list[str] = []
        if params.get("test_command"):
            commands.append(str(params["test_command"]))
        verify_commands = params.get("verify_commands")
        if isinstance(verify_commands, str):
            commands.append(verify_commands)
        elif isinstance(verify_commands, (list, tuple)):
            commands.extend(str(c) for c in verify_commands)

        checks: list[VerificationCheck] = []
        evidence_ids: list[str] = []
        if commands:
            sub = self.verify_code(commands, cwd=params.get("cwd"), task_id=task.id)
            checks.extend(sub.checks)
            evidence_ids.append(sub.id)
            summary = sub.summary
        elif task.artifact_ids:
            for art_id in task.artifact_ids:
                artifact = next((a for a in self.state.artifacts if a.id == art_id), None)
                if artifact is None:
                    checks.append(VerificationCheck(name=f"artifact:{art_id}", status=FAILED, detail="artifact not found in mission state"))
                    continue
                sub = self.verify_artifact(artifact)
                evidence_ids.append(sub.id)
                checks.append(VerificationCheck(name=f"artifact:{artifact.name}", status=sub.status, detail=sub.summary))
            summary = f"{len(task.artifact_ids)} artifact(s): {_counts(checks)}"
        else:
            checks.append(VerificationCheck(name="output", status=INCONCLUSIVE, detail="no verifiable output declared"))
            summary = "no verifiable output declared"
        status = _aggregate(checks)
        result = VerificationResult(target_type="task", target_id=task.id, status=status, summary=summary, checks=checks, evidence_ids=evidence_ids)
        if result.id not in task.verification_attempt_ids:
            task.verification_attempt_ids.append(result.id)
        cite(task.verification_ids, result, task.id)
        return self.record(result)

    # -- mission gate ---------------------------------------------------------------

    def mission_completion_check(self, state: Optional[MissionState] = None) -> VerificationResult:
        return self.record(mission_completion_check(state or self.state))


def mission_completion_check(state: MissionState) -> VerificationResult:
    """Completion-integrity gate: refuse completion until every obligation is verified."""
    checks: list[VerificationCheck] = []
    missing: list[str] = []

    # A criterion counts as verified only when an id on it resolves to a PASSED record in
    # durable state. A non-empty id list is not evidence of anything.
    def receipts(c: SuccessCriterion) -> list[VerificationResult]:
        # F4: the receipt must have been produced *against this criterion*. A passing record
        # for some unrelated artifact is not evidence, however it came to be cited.
        return state.passing_verifications(c.verification_ids, target_type="criterion", target_id=c.id)

    unverified = [c for c in state.success_criteria if not (c.satisfied and receipts(c))]
    if not state.success_criteria:
        checks.append(VerificationCheck(name="success_criteria", status=INCONCLUSIVE, detail="mission declares no success criteria"))
        missing.append("no success criteria declared")
    elif unverified:
        detail = "; ".join(f"'{c.description}'" + (" (no passing verification record bound to it)" if not receipts(c) else " (not satisfied)") for c in unverified)
        checks.append(VerificationCheck(name="success_criteria", status=FAILED, detail=detail))
        missing.append(f"{len(unverified)} of {len(state.success_criteria)} success criteria not verified: {detail}")
    else:
        checks.append(VerificationCheck(name="success_criteria", status=PASSED, detail=f"all {len(state.success_criteria)} criteria satisfied and verified"))

    # Only the latest record per test command counts: a fixed failure is not a failure.
    latest: dict[str, Any] = {}
    for rec in state.tests:
        if rec.expected_failure:
            continue  # a reproduction run is evidence the bug exists, never that the suite passes
        latest[rec.name] = rec
    failing_tests = [t for t in latest.values() if t.status == FAILED]
    if failing_tests:
        names = ", ".join(t.name for t in failing_tests[:5])
        checks.append(VerificationCheck(name="tests", status=FAILED, detail=f"{len(failing_tests)} failing test record(s): {names}"))
        missing.append(f"failing tests: {names}")
    else:
        checks.append(VerificationCheck(name="tests", status=PASSED, detail=f"{len(state.tests)} test record(s), none failing"))

    contradictions = [c for c in state.unresolved_contradictions() if c.severity >= CONTRADICTION_SEVERITY_GATE]
    if contradictions:
        ids = ", ".join(f"{c.id} (severity {c.severity:.2f})" for c in contradictions[:5])
        checks.append(VerificationCheck(name="contradictions", status=FAILED, detail=ids))
        missing.append(f"unresolved contradictions with severity >= {CONTRADICTION_SEVERITY_GATE}: {ids}")
    else:
        checks.append(VerificationCheck(name="contradictions", status=PASSED, detail="no unresolved contradictions above severity gate"))

    blocked = []
    for b in state.blocked_operations:
        if b.resolved or not b.task_id:
            continue
        task = state.task(b.task_id)
        if task is None or task.status != TaskStatus.DONE:
            blocked.append(b)
    if blocked:
        detail = "; ".join(f"{b.operation} (task {b.task_id}: {b.what_would_unblock})" for b in blocked[:5])
        checks.append(VerificationCheck(name="blocked_operations", status=FAILED, detail=detail))
        missing.append(f"unresolved blocked operations on unfinished tasks: {detail}")
    else:
        checks.append(VerificationCheck(name="blocked_operations", status=PASSED, detail="no unresolved blocked operations on unfinished tasks"))

    waiting = [h for h in state.unanswered_human_requests() if not h.independent_work_remaining]
    if waiting:
        detail = "; ".join(f"{h.kind}: {h.question}" for h in waiting[:5])
        checks.append(VerificationCheck(name="human_requests", status=FAILED, detail=detail))
        missing.append(f"unanswered human requests with no independent work remaining: {detail}")
    else:
        checks.append(VerificationCheck(name="human_requests", status=PASSED, detail="no blocking unanswered human requests"))

    # F3: `verified` is a claim about the past. Re-hash every artifact the mission still
    # presents as verified, so a file deleted or edited after verification cannot pass the gate.
    intact: dict[str, Artifact] = {}
    broken: list[str] = []
    for a in state.artifacts:
        if not a.verified:
            continue
        ok, detail = artifact_integrity(a)
        if ok:
            intact[a.id] = a
        else:
            broken.append(f"'{a.name}' ({detail})")
    if broken:
        checks.append(VerificationCheck(name="artifact_integrity", status=FAILED, detail="; ".join(broken[:5])))
        missing.append(f"{len(broken)} verified artifact(s) no longer match what was verified: " + "; ".join(broken[:5]))
    elif intact:
        checks.append(VerificationCheck(name="artifact_integrity", status=PASSED, detail=f"all {len(intact)} verified artifacts still match their recorded hash"))
    else:
        checks.append(VerificationCheck(name="artifact_integrity", status=SKIPPED, detail="no verified artifacts to re-check"))

    required = list(state.resources.get("required_artifacts", []) or [])
    if required:
        # A required artifact may be declared by id, by name, or by path — the live mission
        # declared absolute paths, which matched nothing, so even a verified intact artifact at
        # exactly that path could not satisfy the check. Compare all three, resolving paths so an
        # equivalent spelling of the same file is recognised as the same file.
        verified_keys = set(intact) | {a.name for a in intact.values()}
        verified_paths: set[str] = set()
        for a in intact.values():
            if a.path:
                verified_paths.add(str(a.path))
                try:
                    verified_paths.add(str(Path(a.path).resolve()))
                except OSError:
                    pass

        def _declared_present(ref: str) -> bool:
            if ref in verified_keys or ref in verified_paths:
                return True
            try:
                return str(Path(ref).resolve()) in verified_paths
            except OSError:
                return False

        absent = [r for r in required if not _declared_present(str(r))]
        if absent:
            checks.append(VerificationCheck(name="required_artifacts", status=FAILED, detail="missing or unverified: " + ", ".join(map(str, absent))))
            missing.append("required artifacts missing or unverified: " + ", ".join(map(str, absent)))
        else:
            checks.append(VerificationCheck(name="required_artifacts", status=PASSED, detail=f"all {len(required)} required artifacts verified and intact"))
    else:
        checks.append(VerificationCheck(name="required_artifacts", status=SKIPPED, detail="no required artifacts declared"))

    # A receipt proves something about the bytes it was produced against. Before accepting one at
    # completion, re-read those inputs: if they have moved, the receipt still describes what it
    # saw (history is kept) but it no longer describes what is there, so it cannot close a
    # criterion. Records written before version binding carry no input_versions at all; that is
    # "unknown", and unknown is not re-interpreted as "unchanged".
    stale: list[str] = []
    unbound: list[str] = []
    for c in state.success_criteria:
        crs = receipts(c)
        if not crs:
            continue
        # A criterion proved by a test run the current engine produced must name the inputs that
        # run was about, or nothing can be re-checked later. Records with no structured counts
        # predate version binding and keep the older, weaker guarantee rather than being
        # retroactively failed.
        producing_tasks = {t.id for t in state.tasks if c.id in (t.addresses_criterion_ids or [])}
        engine_backed = any(
            t.status == PASSED and t.counts and (c.id in (t.criterion_ids or []) or (t.task_id in producing_tasks))
            for t in state.tests
        )
        judged = [(receipt_inputs_intact(r), r) for r in crs]
        live = [r for (ok, _why), r in judged if ok and (r.input_versions or not engine_backed)]
        if live:
            # A later receipt that still matches supersedes an earlier one that no longer does.
            # Re-verifying after a fix is exactly how a mission is meant to recover; the stale
            # record stays in history rather than blocking forever.
            continue
        broken_reasons = [why for (ok, why), _r in judged if not ok]
        if broken_reasons:
            stale.append(f"'{c.description[:60]}': no receipt still matches the current inputs — " + "; ".join(broken_reasons[:2]))
        elif engine_backed:
            unbound.append(f"'{c.description[:60]}' is proved by a test run that names no input versions, so it cannot be re-checked")
    if stale or unbound:
        detail = "; ".join((stale + unbound)[:5])
        checks.append(VerificationCheck(name="receipt_input_versions", status=FAILED, detail=detail, authoritative=True))
        if stale:
            missing.append(f"{len(stale)} receipt(s) no longer describe the current inputs: " + "; ".join(stale[:3]))
        if unbound:
            missing.append(f"{len(unbound)} criterion receipt(s) name no input versions: " + "; ".join(unbound[:3]))
    else:
        bound = sum(len(r.input_versions) for c in state.success_criteria for r in receipts(c))
        checks.append(
            VerificationCheck(
                name="receipt_input_versions",
                status=PASSED,
                detail=f"{bound} bound input version(s) re-read and still matching",
                authoritative=True,
            )
        )

    status = _aggregate(checks)
    if status == PASSED:
        summary = "all completion gates satisfied"
    else:
        summary = "completion blocked: " + " | ".join(missing) if missing else f"completion not confirmed: {_counts(checks)}"
    return VerificationResult(target_type="mission", target_id=state.mission_id, status=status, summary=summary, checks=checks)
