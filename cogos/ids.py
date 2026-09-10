"""Identifier and time helpers shared across the runtime.

Identifiers are prefixed, time-sortable, and collision resistant without
requiring an external ULID dependency: ``<prefix>_<ms-timestamp-base32>-<random>``.
"""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford base32 (lowercase)


def _b32(value: int, width: int) -> str:
    out = []
    for _ in range(width):
        out.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(out))


def new_id(prefix: str) -> str:
    """Return a prefixed, lexicographically time-ordered identifier."""
    ms = int(time.time() * 1000)
    return f"{prefix}_{_b32(ms, 9)}{secrets.token_hex(4)}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="milliseconds")
