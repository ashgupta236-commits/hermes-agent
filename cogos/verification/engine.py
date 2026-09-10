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
from cogos.schemas.verification import VerificationCheck, VerificationResult
from cogos.tools.fabric import ToolFabric

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

    MAX_DURABLE_VERIFICATIONS = 500

    def record(self, result: VerificationResult) -> VerificationResult:
        """Persist a verification record into mission state so gates can resolve it later."""
        self.results.append(result)
        if self.state is not None:
            if not any(v.id == result.id for v in self.state.verifications):
                self.state.verifications.append(result)
            del self.state.verifications[: -self.MAX_DURABLE_VERIFICATIONS]
        return result

    # -- code -----------------------------------------------------------------------

    def verify_code(self, commands: Optional[list[str]] = None, cwd: Optional[str] = None, task_id: Optional[str] = None) -> VerificationResult:
        commands = list(commands) if commands else list(DEFAULT_TEST_COMMANDS)
        checks: list[VerificationCheck] = []
        test_totals = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
        if self.fabric is None:
            for cmd in commands:
                checks.append(VerificationCheck(name=cmd, status=INCONCLUSIVE, detail="no tool fabric"))
                self.state.tests.append(TestRecord(name=cmd, command=cmd, status=SKIPPED, summary="no tool fabric", ran_at=iso_now(), task_id=task_id))
            return self.record(
                VerificationResult(target_type="code", target_id=task_id or "code", status=INCONCLUSIVE, summary="no tool fabric: tests not run", checks=checks)
            )
        for cmd in commands:
            shell_cmd = f"cd {shlex.quote(str(cwd))} && {cmd}" if cwd else cmd
            res = self.fabric.execute(ToolCall(tool="run_tests", arguments={"command": shell_cmd}, task_id=task_id, purpose="verification"))
            for key, value in (res.data.get("counts") or {}).items():
                if key in test_totals:
                    test_totals[key] += int(value)
            if res.ok:
                status = PASSED
            elif res.error_kind in ("denied", "requires_human", "unavailable"):
                status = INCONCLUSIVE
            else:
                status = FAILED
            tail = res.output.strip().splitlines()
            detail = str(res.data.get("summary") or res.error or (tail[-1] if tail else ""))
            checks.append(VerificationCheck(name=cmd, status=status, detail=detail))
            self.state.tests.append(TestRecord(name=cmd, command=shell_cmd, status=status, summary=detail, ran_at=iso_now(), task_id=task_id))
        status = _aggregate(checks)
        n_pass = sum(1 for c in checks if c.status == PASSED)
        n_fail = sum(1 for c in checks if c.status == FAILED)
        n_inc = len(checks) - n_pass - n_fail
        summary = f"{n_pass}/{len(checks)} commands passed, {n_fail} failed, {n_inc} inconclusive; tests: {test_totals['passed']} passed, {test_totals['failed']} failed, {test_totals['error']} errors"
        return self.record(VerificationResult(target_type="code", target_id=task_id or "code", status=status, summary=summary, checks=checks))

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

    def verify_research(self, claim_ids: Optional[list[str]] = None) -> VerificationResult:
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
            VerificationResult(target_type="claim", target_id=",".join(ids) if ids else "claims", status=status, summary=summary, checks=checks, evidence_ids=sorted(set(evidence_ids)))
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
            checks.append(VerificationCheck(name="path", status=FAILED, detail="artifact declares no path"))
        elif not path.exists():
            checks.append(VerificationCheck(name="path", status=FAILED, detail=f"{path} does not exist"))
        elif not path.is_file():
            checks.append(VerificationCheck(name="path", status=FAILED, detail=f"{path} is not a regular file"))
        else:
            checks.append(VerificationCheck(name="path", status=PASSED, detail=str(path)))
            size = path.stat().st_size
            if size == 0:
                checks.append(VerificationCheck(name="non_empty", status=FAILED, detail="file is empty"))
            else:
                checks.append(VerificationCheck(name="non_empty", status=PASSED, detail=f"{size} bytes"))
            digest = hashlib.sha256()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    digest.update(chunk)
            content_hash = digest.hexdigest()
            previous = artifact.content_hash
            artifact.content_hash = content_hash
            if previous and previous != content_hash:
                checks.append(VerificationCheck(name="hash", status=INCONCLUSIVE, detail=f"sha256 {content_hash[:12]}… differs from previously recorded {previous[:12]}…"))
            else:
                checks.append(VerificationCheck(name="hash", status=PASSED, detail=f"sha256 {content_hash}"))
        status = _aggregate(checks)
        artifact.verified = status == PASSED
        stored = next((a for a in self.state.artifacts if a.id == artifact.id), None)
        if stored is not None and stored is not artifact:
            stored.content_hash = artifact.content_hash
            stored.verified = artifact.verified
        summary = f"artifact '{artifact.name}': {_counts(checks)}"
        return self.record(VerificationResult(target_type="artifact", target_id=artifact.id, status=status, summary=summary, checks=checks))

    # -- criteria -------------------------------------------------------------------

    def verify_criterion(self, criterion: SuccessCriterion, evidence_ok: Optional[bool] = None) -> VerificationResult:
        method = (criterion.verification_method or "").lower()
        checks: list[VerificationCheck] = []
        evidence_ids: list[str] = []
        created_ms = _id_timestamp_ms(criterion.id) or 0

        if re.search(r"\b(tests?|pytest|unit\s*tests?)\b", method):
            fresh = [t for t in self.state.tests if t.status == PASSED and not t.expected_failure and (_iso_to_ms(t.ran_at) or -1) >= created_ms]
            if fresh:
                checks.append(VerificationCheck(name="tests", status=PASSED, detail=f"{len(fresh)} passed test record(s) newer than criterion: " + ", ".join(t.name for t in fresh[:3])))
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
        result = VerificationResult(
            target_type="criterion",
            target_id=criterion.id,
            status=status,
            summary=f"criterion '{criterion.description}': {_counts(checks)}",
            checks=checks,
            evidence_ids=sorted(set(evidence_ids)),
        )
        # Only a passing record is citable evidence. A failed or inconclusive attempt is still
        # persisted (see record()), but citing it would turn the completion gate into a
        # has-this-been-attempted check.
        if status == PASSED and result.id not in criterion.verification_ids:
            criterion.verification_ids.append(result.id)
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
        task.verification_ids.append(result.id)
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
    unverified = [c for c in state.success_criteria if not (c.satisfied and state.passing_verifications(c.verification_ids))]
    if not state.success_criteria:
        checks.append(VerificationCheck(name="success_criteria", status=INCONCLUSIVE, detail="mission declares no success criteria"))
        missing.append("no success criteria declared")
    elif unverified:
        detail = "; ".join(f"'{c.description}'" + (" (no passing verification record)" if not state.passing_verifications(c.verification_ids) else " (not satisfied)") for c in unverified)
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

    required = list(state.resources.get("required_artifacts", []) or [])
    if required:
        verified_keys = {a.id for a in state.artifacts if a.verified} | {a.name for a in state.artifacts if a.verified}
        absent = [r for r in required if str(r) not in verified_keys]
        if absent:
            checks.append(VerificationCheck(name="required_artifacts", status=FAILED, detail="missing or unverified: " + ", ".join(map(str, absent))))
            missing.append("required artifacts missing or unverified: " + ", ".join(map(str, absent)))
        else:
            checks.append(VerificationCheck(name="required_artifacts", status=PASSED, detail=f"all {len(required)} required artifacts verified"))
    else:
        checks.append(VerificationCheck(name="required_artifacts", status=SKIPPED, detail="no required artifacts declared"))

    status = _aggregate(checks)
    if status == PASSED:
        summary = "all completion gates satisfied"
    else:
        summary = "completion blocked: " + " | ".join(missing) if missing else f"completion not confirmed: {_counts(checks)}"
    return VerificationResult(target_type="mission", target_id=state.mission_id, status=status, summary=summary, checks=checks)
