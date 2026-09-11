"""The bounded data protocol between the trusted verifier and the isolated subject.

The subject's process authors everything on this channel. It may emit ``"passed": true``, forged
JUnit, an ``authority`` field, or a whole receipt-shaped object; none of that can change a result,
because the trusted side reads exactly four keys and compares the one that matters against a value
the subject never sees.

Rules this module enforces, each because the alternative is a hole:

* **Data only.** ``json.loads`` with a pairs hook. No pickle, no ``eval``, no dynamic import, no
  object hooks that construct types. A response is scalars or it is rejected.
* **Fixed vocabulary.** Operation names come from an allowlist. An unknown ``op`` is a protocol
  error, not an extension point.
* **Request identity, used once.** A response must name a request the verifier actually issued and
  has not already matched. That is what makes a replayed transcript a protocol error rather than a
  result.
* **Finite numbers.** ``NaN`` and infinities are refused: JSON does not have them, Python's decoder
  invents them, and ``NaN != NaN`` silently turns a comparison into a pass-shaped non-answer.
* **Bounded everything.** Line length, total bytes, response count and diagnostic size are all
  capped, so a subject cannot exhaust the controller by talking.
* **Explicit absence.** A missing response is a distinct outcome from a wrong one. Neither is a
  pass.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

#: Bumping this is a breaking change to the subject adapter and to every receipt that cites it.
PROTOCOL_VERSION = "cogos.behaviour.v1"

#: Printed by the adapter once it has imported the deliverable and before it reads a single request.
#: The verifier withholds stdin until it sees this. Module-level code in the subject runs *before*
#: the adapter's loop, so a deliverable that reads stdin at import time can answer the protocol
#: itself — with the inputs in hand and the requirement in front of it, it can return correct values
#: while shipping an `add_percent` that is wrong. Withholding the requests until after the import
#: turns that attack into a subject blocked on an empty pipe, and the run times out.
READY_MARKER = '{"protocol": "%s", "ready": true}' % PROTOCOL_VERSION

#: Operations the verifier knows how to state an expectation about. An `op` outside this set cannot
#: be requested and cannot be answered.
SUPPORTED_OPS = frozenset({"add_percent"})

MAX_LINE_BYTES = 8 * 1024
MAX_TOTAL_RESPONSE_BYTES = 512 * 1024
MAX_RESPONSES = 512
MAX_DIAGNOSTIC_BYTES = 8 * 1024
#: Beyond this the value is not a number the requirement is about; it is an attempt to make the
#: comparison expensive or to smuggle a float that does not round-trip.
MAX_ABS_VALUE = 1e12


class ProtocolError(Exception):
    """The response channel violated the contract. Never a verdict about behaviour."""


@dataclass(frozen=True)
class Request:
    request_id: str
    op: str
    args: dict[str, float]

    def encode(self) -> str:
        if self.op not in SUPPORTED_OPS:
            raise ProtocolError(f"unsupported op {self.op!r}")
        return json.dumps(
            {"protocol": PROTOCOL_VERSION, "request_id": self.request_id, "op": self.op, "args": dict(self.args)},
            sort_keys=True,
            allow_nan=False,
        )


@dataclass(frozen=True)
class Response:
    request_id: str
    ok: bool
    result: Optional[float] = None
    error: str = ""


@dataclass
class Transcript:
    """What came back, and every way it was malformed."""

    responses: dict[str, Response] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def missing(self, request_ids: Iterable[str]) -> list[str]:
        return [r for r in request_ids if r not in self.responses]


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            # A duplicate key means two different readers of this message can disagree about what it
            # says, which is exactly the ambiguity a wire format must not have.
            raise ProtocolError(f"duplicate key {key!r}")
        seen.add(key)
    return dict(pairs)


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        # A bare JSON integer of a few hundred digits raises OverflowError here. It is an
        # ArithmeticError, so a `except ValueError` further out does not catch it, and it used to
        # propagate all the way out of the verification run — a subject killing the verifier by
        # typing a long number.
        return None
    if not math.isfinite(number) or abs(number) > MAX_ABS_VALUE:
        return None
    return number


def sanitise(text: str, limit: int) -> str:
    """Make subject-authored text safe to put in durable mission state.

    `json.loads` happily materialises an unpaired surrogate from `\\ud800`, and such a string cannot
    be serialised again — so a subject could write seven ASCII characters into an error message and
    make the mission unsaveable. Round-tripping through UTF-8 with replacement removes the class.
    """
    return text.encode("utf-8", "replace").decode("utf-8", "replace")[:limit]


def decode_transcript(raw: str, issued: Iterable[str]) -> Transcript:
    """Parse the subject's stdout into responses, refusing anything the contract does not allow.

    `issued` is the set of request ids the verifier actually sent. A response naming anything else
    — including a well-formed response to a question nobody asked — is discarded with a reason.
    """
    expected = set(issued)
    transcript = Transcript()
    encoded = raw.encode("utf-8", "replace")
    if len(encoded) > MAX_TOTAL_RESPONSE_BYTES:
        transcript.problems.append(f"response stream exceeded {MAX_TOTAL_RESPONSE_BYTES} bytes")
        # Truncate the *bytes*, then decode: slicing characters by a byte count keeps up to four
        # times the advertised bound, and this function's docstring is what a caller relies on.
        raw = encoded[:MAX_TOTAL_RESPONSE_BYTES].decode("utf-8", "replace")

    lines = [line for line in raw.splitlines() if line.strip()]
    if len(lines) > MAX_RESPONSES:
        transcript.problems.append(f"more than {MAX_RESPONSES} response lines")
        lines = lines[:MAX_RESPONSES]

    for index, line in enumerate(lines):
        if len(line.encode("utf-8", "replace")) > MAX_LINE_BYTES:
            transcript.problems.append(f"line {index} exceeded {MAX_LINE_BYTES} bytes")
            continue
        try:
            message = json.loads(line, object_pairs_hook=_no_duplicate_keys)
        except ProtocolError as exc:
            transcript.problems.append(f"line {index}: {exc}")
            continue
        except (ValueError, RecursionError):
            # Not JSON, or nested past the decoder's limit. Subject processes print all sorts of
            # things; a line that is not a message is not an error, it is not a message.
            continue
        if not isinstance(message, dict):
            continue
        if message.get("protocol") != PROTOCOL_VERSION:
            continue
        if message.get("ready") is True and "request_id" not in message:
            # The handshake line, not a response. Without this it parses as a response to no
            # request and is recorded as a protocol problem — which made every honest run fail.
            continue

        request_id = message.get("request_id")
        if not isinstance(request_id, str) or request_id not in expected:
            transcript.problems.append(f"line {index}: response to an unissued request id")
            continue
        if request_id in transcript.responses:
            # Replay, or two answers to one question. Either way there is no honest way to pick.
            transcript.problems.append(f"line {index}: duplicate response for {request_id}")
            continue

        ok = message.get("ok")
        if not isinstance(ok, bool):
            transcript.problems.append(f"line {index}: 'ok' is not a boolean")
            continue
        if not ok:
            error = sanitise(str(message.get("error", "")), MAX_DIAGNOSTIC_BYTES)
            transcript.responses[request_id] = Response(request_id=request_id, ok=False, error=error)
            continue
        result = _finite(message.get("result"))
        if result is None:
            transcript.problems.append(f"line {index}: 'result' is not a finite number in range")
            continue
        transcript.responses[request_id] = Response(request_id=request_id, ok=True, result=result)

    return transcript


#: The adapter that runs **inside** the sandbox. Engine-authored and materialised into the snapshot,
#: never taken from the workspace — but it executes in the subject's process, so everything it
#: prints is untrusted. Its presence authenticates nothing; the verifier's authority comes from
#: comparing what comes back against expectations the subject never receives.
SUBJECT_ADAPTER = '''\
"""cogos subject adapter — runs inside the isolation boundary. Its output is untrusted."""
import json
import math
import sys

PROTOCOL = "cogos.behaviour.v1"
READY_LINE = '{"protocol": "cogos.behaviour.v1", "ready": true}'


def _emit(request_id, ok, result=None, error=""):
    message = {"protocol": PROTOCOL, "request_id": request_id, "ok": bool(ok)}
    if ok:
        message["result"] = result
    else:
        message["error"] = str(error)[:2000]
    sys.stdout.write(json.dumps(message) + "\\n")
    sys.stdout.flush()


def main():
    sys.path.insert(0, "/subject")
    try:
        import calc
    except BaseException as exc:  # noqa: BLE001 - any import failure is a reportable outcome
        calc = None
        import_error = "%s: %s" % (type(exc).__name__, exc)
    else:
        import_error = ""

    # Announce only after the import has completed. The verifier withholds the requests until it
    # sees this, so module-level code in the subject cannot read them.
    sys.stdout.write(READY_LINE + "\\n")
    sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            request_id = request["request_id"]
            op = request["op"]
            args = request["args"]
        except BaseException:  # noqa: BLE001
            continue
        if calc is None:
            _emit(request_id, False, error="import failed: " + import_error)
            continue
        try:
            function = getattr(calc, op)
            value = function(args["value"], args["percent"])
        except BaseException as exc:  # noqa: BLE001
            _emit(request_id, False, error="%s: %s" % (type(exc).__name__, exc))
            continue
        try:
            number = float(value)
        except BaseException:  # noqa: BLE001
            _emit(request_id, False, error="result is not a number: %r" % (value,))
            continue
        if not math.isfinite(number):
            _emit(request_id, False, error="result is not finite")
            continue
        _emit(request_id, True, result=number)


if __name__ == "__main__":
    main()
'''
