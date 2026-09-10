"""Capability firewall: a policy layer distinct from cognition.

Every tool call is classified into an :class:`ActionClass` and checked against
the configured governance policy. Verdicts are ALLOW, DENY, or REQUIRE_HUMAN.
The firewall never touches the executive model choice — a denial narrows the
action space and nothing else.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from cogos.config import GovernanceConfig
from cogos.schemas.common import ActionClass, PolicyDecision
from cogos.schemas.tools import FirewallVerdict, ToolCall, ToolSpec

_DESTRUCTIVE = re.compile(
    r"(^|[\s;&|])(rm\s+-[a-z]*r[a-z]*f?|rm\s+-[a-z]*f[a-z]*r|rmdir|mkfs|dd\s+if=|shred|truncate\s+-s\s*0|:\(\)\s*\{|git\s+(push\s+.*--force|push\s+-f|reset\s+--hard|clean\s+-[a-z]*f|branch\s+-D)|drop\s+(table|database)|delete\s+from\b|kill\s+-9\s+-1)",
    re.I,
)
_FINANCIAL = re.compile(r"\b(stripe|paypal|braintree|charge|payment|purchase|buy_domain|invoice\.pay|transfer\s+funds)\b", re.I)
_CREDENTIAL = re.compile(r"(\.env\b|id_rsa|id_ed25519|\.pem\b|\.ppk\b|credentials\.json|\.aws/credentials|\.netrc|keychain|secret[_-]?key|api[_-]?key=)", re.I)
_SECURITY = re.compile(r"\b(chmod\s+[0-7]*7[0-7]*\s+/|chown\s+-R\s+root|sudo\b|iptables|ufw\b|setcap|visudo|passwd\b|ssh-keygen|openssl\s+(req|genrsa))", re.I)
_EXTERNAL_WRITE = re.compile(r"\b(git\s+push|curl\s+.*(-X\s*(POST|PUT|PATCH|DELETE)|--data|-d\s)|wget\s+--post|scp\b|rsync\s+.*:|ssh\b|gh\s+(pr|release|issue)\s+(create|merge|close)|npm\s+publish|twine\s+upload|docker\s+push|kubectl\s+(apply|delete)|terraform\s+(apply|destroy))\b", re.I)
_PRIVACY = re.compile(r"\b(ssn|social\s+security|passport\s+number|medical\s+record|date\s+of\s+birth)\b", re.I)
_LEGAL = re.compile(r"\b(sign\s+(the\s+)?(contract|agreement)|accept\s+(the\s+)?terms|legally\s+binding|notariz)", re.I)
_NETWORK_READ = re.compile(r"\b(curl|wget|http|pip\s+install|npm\s+install|uv\s+(pip|add|sync)|apt(-get)?\s+install|brew\s+install)\b", re.I)


def classify_shell_command(command: str) -> ActionClass:
    """Classify a shell command by its most severe recognisable effect."""
    c = command.strip()
    if _DESTRUCTIVE.search(c):
        return ActionClass.DESTRUCTIVE
    if _FINANCIAL.search(c):
        return ActionClass.FINANCIAL
    if _LEGAL.search(c):
        return ActionClass.LEGALLY_SIGNIFICANT
    if _CREDENTIAL.search(c):
        return ActionClass.CREDENTIAL_SENSITIVE
    if _SECURITY.search(c):
        return ActionClass.SECURITY_SENSITIVE
    if _PRIVACY.search(c):
        return ActionClass.PRIVACY_SENSITIVE
    if _EXTERNAL_WRITE.search(c):
        return ActionClass.CONSEQUENTIAL_SHARED
    if _NETWORK_READ.search(c):
        return ActionClass.REVERSIBLE_EXTERNAL
    return ActionClass.REVERSIBLE_LOCAL


def _path_within(path: Path, roots: list[Path]) -> bool:
    try:
        rp = path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            rp.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


class CapabilityFirewall:
    def __init__(self, config: GovernanceConfig, repo_root: Path, extra_writable: Optional[list[Path]] = None, human_grants: Optional[set[str]] = None):
        self.config = config
        self.repo_root = Path(repo_root)
        roots = [Path(p) for p in config.writable_roots] or [self.repo_root]
        self.writable_roots = roots + list(extra_writable or [])
        # action classes the human has explicitly authorised for this mission
        self.human_grants: set[str] = set(human_grants or set())
        self.audit: list[FirewallVerdict] = []

    # -- classification ------------------------------------------------------------

    def classify(self, call: ToolCall, spec: ToolSpec) -> ActionClass:
        args = call.arguments
        if spec.substrate == "shell":
            return classify_shell_command(str(args.get("command", "")))
        if spec.substrate == "git":
            argv = args.get("args") or []
            joined = "git " + (" ".join(map(str, argv)) if isinstance(argv, list) else str(argv))
            return classify_shell_command(joined)
        if spec.substrate == "filesystem" and spec.name in ("write_file", "delete_file", "append_file"):
            target = Path(str(args.get("path", "")))
            if not target.is_absolute():
                target = self.repo_root / target
            if _CREDENTIAL.search(str(target)):
                return ActionClass.CREDENTIAL_SENSITIVE
            if spec.name == "delete_file":
                return ActionClass.DESTRUCTIVE if not _path_within(target, self.writable_roots) else ActionClass.REVERSIBLE_LOCAL
            return ActionClass.REVERSIBLE_LOCAL if _path_within(target, self.writable_roots) else ActionClass.CONSEQUENTIAL_SHARED
        if spec.substrate == "web":
            method = str(args.get("method", "GET")).upper()
            if method != "GET":
                return ActionClass.CONSEQUENTIAL_SHARED
            return ActionClass.REVERSIBLE_EXTERNAL
        return spec.default_action_class

    # -- policy ----------------------------------------------------------------------

    def check(self, call: ToolCall, spec: ToolSpec) -> FirewallVerdict:
        action_class = self.classify(call, spec)
        verdict = self._decide(call, spec, action_class)
        self.audit.append(verdict)
        return verdict

    def _decide(self, call: ToolCall, spec: ToolSpec, action_class: ActionClass) -> FirewallVerdict:
        cfg = self.config
        if not spec.available:
            return FirewallVerdict(decision=PolicyDecision.DENY, action_class=action_class, reason=f"tool unavailable: {spec.unavailable_reason or 'not configured'}")
        if action_class.value in cfg.denied_action_classes:
            return FirewallVerdict(decision=PolicyDecision.DENY, action_class=action_class, reason=f"action class '{action_class.value}' is denied by policy")
        if spec.substrate == "shell" and not cfg.allow_shell:
            return FirewallVerdict(decision=PolicyDecision.DENY, action_class=action_class, reason="shell execution disabled by policy")
        if spec.network:
            if not cfg.allow_network:
                return FirewallVerdict(decision=PolicyDecision.DENY, action_class=action_class, reason="network access disabled by policy")
            url = str(call.arguments.get("url", ""))
            if url:
                host = urlparse(url).netloc.lower()
                if any(host == d or host.endswith("." + d) for d in cfg.denied_domains):
                    return FirewallVerdict(decision=PolicyDecision.DENY, action_class=action_class, reason=f"domain {host} denied by policy")
                if cfg.allowed_domains and not any(host == d or host.endswith("." + d) for d in cfg.allowed_domains):
                    return FirewallVerdict(decision=PolicyDecision.DENY, action_class=action_class, reason=f"domain {host} not in allowed_domains")
        if action_class.value in cfg.always_require_human and action_class.value not in self.human_grants:
            return FirewallVerdict(
                decision=PolicyDecision.REQUIRE_HUMAN,
                action_class=action_class,
                reason=f"action class '{action_class.value}' requires explicit human authorization",
            )
        if action_class in (ActionClass.CONSEQUENTIAL_SHARED, ActionClass.SECURITY_SENSITIVE, ActionClass.PRIVACY_SENSITIVE):
            if action_class.value not in self.human_grants and action_class.value in cfg.always_require_human:
                return FirewallVerdict(decision=PolicyDecision.REQUIRE_HUMAN, action_class=action_class, reason=f"'{action_class.value}' requires authorization")
        # Filesystem writes outside writable roots are consequential/shared: deny unless granted.
        if spec.substrate == "filesystem" and action_class == ActionClass.CONSEQUENTIAL_SHARED and "consequential_shared" not in self.human_grants:
            return FirewallVerdict(decision=PolicyDecision.DENY, action_class=action_class, reason="write outside writable roots")
        return FirewallVerdict(decision=PolicyDecision.ALLOW, action_class=action_class, reason="within policy")

    def grant(self, action_class: str) -> None:
        self.human_grants.add(action_class)

    @staticmethod
    def shell_argv(command: str) -> list[str]:
        try:
            return shlex.split(command)
        except ValueError:
            return command.split()
