"""Cognitive immune system: external information is data, not authority.

Provides injection scanning, trust scoring for sources, and helpers to detect
circular sourcing. Nothing here calls a model; it is deterministic and fast
so it can run on every tool output.
"""

from __future__ import annotations

import re
from typing import Iterable
from urllib.parse import urlparse

from cogos.adapters.base import UntrustedBlock

# Patterns that indicate content is attempting to instruct the agent.
_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ignore_previous", re.compile(r"ignore (all |any )?(previous|prior|above) (instructions|directions|rules)", re.I)),
    ("role_override", re.compile(r"\byou are now\b|\bact as (an? )?(system|administrator|root)\b|\bnew system prompt\b", re.I)),
    ("system_tag", re.compile(r"<\s*/?\s*(system|assistant|instructions?)\s*>", re.I)),
    ("exfiltration", re.compile(r"(send|post|upload|exfiltrate|leak)\s+(the\s+)?(api[_ ]?key|token|secret|credential|password|\.env)", re.I)),
    ("tool_command", re.compile(r"\b(run|execute)\s+(the\s+)?(following|this)\s+(command|shell|script)\b", re.I)),
    ("destructive", re.compile(r"\brm\s+-rf\b|\bgit\s+push\s+--force\b|\bdrop\s+table\b|\bformat\s+c:", re.I)),
    ("authority_claim", re.compile(r"\b(this is|message from) (the )?(developer|anthropic|openai|administrator|your operator)\b", re.I)),
    ("override_mission", re.compile(r"\b(disregard|abandon|forget)\s+(the\s+)?(mission|objective|task)\b", re.I)),
    ("hidden_text", re.compile(r"[​‌‍⁠﻿]{3,}")),
    ("permission_bypass", re.compile(r"dangerously[-_ ]skip[-_ ]permissions|bypass\s*permissions|disable (the )?(firewall|sandbox|safety)", re.I)),
]


def scan_for_injection(text: str) -> list[str]:
    """Return the list of injection pattern names found in ``text``."""
    if not text:
        return []
    flags = [name for name, pat in _INJECTION_PATTERNS if pat.search(text)]
    return flags


def wrap_untrusted(label: str, source: str, content: str, max_chars: int = 20_000) -> UntrustedBlock:
    trimmed = content if len(content) <= max_chars else content[:max_chars] + "\n…[truncated]"
    return UntrustedBlock(label=label, source=source, content=trimmed, injection_flags=scan_for_injection(trimmed))


_HIGH_TRUST_HOSTS = (
    "gov",
    "edu",
    "who.int",
    "worldbank.org",
    "imf.org",
    "oecd.org",
    "un.org",
    "europa.eu",
    "arxiv.org",
    "nature.com",
    "science.org",
    "sec.gov",
    "docs.python.org",
    "github.com",
)
_LOW_TRUST_MARKERS = ("blogspot", "medium.com", "quora", "reddit", "pinterest", "facebook", "tiktok")


def source_trust(source: str) -> float:
    """Heuristic prior reliability for a source string (URL/path/tool)."""
    if not source:
        return 0.3
    low = source.lower()
    if low in ("human", "human_principal"):
        return 0.95
    if low.startswith(("tool:", "tests:", "calc:", "file:")):
        return 0.9
    if low.startswith("http"):
        host = urlparse(low).netloc
        if any(host.endswith("." + h) or host == h or host.endswith(h) for h in _HIGH_TRUST_HOSTS):
            return 0.8
        if any(m in host for m in _LOW_TRUST_MARKERS):
            return 0.35
        return 0.5
    if low.startswith("specialist:"):
        return 0.55
    return 0.45


def independent_root_count(lineages: Iterable[Iterable[str]]) -> int:
    """Count distinct root sources across evidence lineages (false-consensus guard)."""
    roots: set[str] = set()
    for lineage in lineages:
        items = [x.strip().lower() for x in lineage if x and x.strip()]
        if items:
            roots.add(items[0])
    return len(roots)


def normalise_source_key(source: str) -> str:
    low = source.strip().lower()
    if low.startswith("http"):
        p = urlparse(low)
        return p.netloc.replace("www.", "") + p.path.rstrip("/")
    return low
