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
from typing import Any, Optional
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
#: A wildcard in a dot-prefixed token can name a credential file without spelling it: `cat .e*`
#: returned the contents of `.env` while the literal `cat .env` required human authorization. A
#: pattern that *could* match one is classified as though it did; `*.py` is unaffected because it
#: does not begin with a dot.
_CREDENTIAL_GLOB = re.compile(r"(?:^|[\s'\"=])\.[A-Za-z0-9_-]*[*?\[]")
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
    if _CREDENTIAL.search(c) or _CREDENTIAL_GLOB.search(c):
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


#: Shell constructs that write somewhere. Argument analysis is *supplementary*: an allowed
#: interpreter can write wherever the process can, so this narrows the obvious routes and the
#: residual risk is stated rather than papered over. Real containment needs filesystem and
#: network boundaries enforced in the worker environment itself.
_REDIRECT_RE = re.compile(r"(?<![0-9<>])>>?\s*(?P<target>(?:\"[^\"]+\")|(?:'[^']+')|[^\s;&|)]+)")
_WRITER_RE = re.compile(
    r"\b(?:cp|mv|install|ln|touch|mkdir|rmdir|rm|truncate|tee|unzip|tar|rsync|chmod|chown)\b(?P<rest>[^;&|]*)",
    re.IGNORECASE,
)
_DD_OF_RE = re.compile(r"\bdd\b[^;&|]*?\bof=(?P<target>[^\s;&|]+)")
_SED_INPLACE_RE = re.compile(r"\bsed\b[^;&|]*?\s-i(?:\.[^\s]*)?\s+(?:[^\s;&|]+\s+)?(?P<target>[^\s;&|]+)")


#: Constructs whose effect cannot be determined from the command text. Each was reproduced as a
#: live bypass of a boundary the firewall enforces on the literal form of the same command:
#: `OUT=<outside>/x; echo pwned > $OUT` wrote outside the writable roots under an ALLOW verdict,
#: `cd <outside> && echo pwned > f.txt` resolved its relative target against the repo root and did
#: the same, `X="rm -rf"; $X doomed` executed a deletion the `destructive` class routes to human
#: authorization, and `V=env; cat .$V` returned the contents of a credential file that
#: `credential_sensitive` also routes to human authorization.
#:
#: Static inspection of a command string is not, and cannot be made into, a security boundary for
#: arbitrary shell execution: the text does not determine the syscalls. So the unanalysable forms
#: are refused rather than guessed at. Real containment belongs in the worker environment — the
#: writable roots as the only writable mounts, or a kernel-enforced policy — and until that
#: exists this narrows the mechanism instead of overstating the parser.
_SUBSTITUTION = re.compile(r"\$[\w{(]|`")
_INLINE_INTERPRETER = re.compile(
    r"\b(?:eval|xargs|(?:ba|z|da)?sh\s+-[a-z]*c|python[\d.]*\s+-[A-Za-z]*c|perl\s+-[A-Za-z]*[eE]|ruby\s+-[A-Za-z]*e|node\s+(?:-e|--eval)|php\s+-r)\b",
    re.I,
)
_CHDIR = re.compile(r"(?:^|[\s;&|(])cd\s", re.I)


def _outside_single_quotes(command: str) -> str:
    """The command with single-quoted spans blanked out.

    A shell performs no expansion inside single quotes, so `awk '{print $1}'` contains no
    variable reference and must not be refused as if it did.
    """
    out: list[str] = []
    quoted = False
    for ch in command:
        if ch == "'":
            quoted = not quoted
            out.append(" ")
        else:
            out.append(" " if quoted else ch)
    return "".join(out)


#: Git is a process spawner. Each of these makes it run a program named somewhere other than the
#: argument list: an alias whose body starts with `!`, a config assignment that can carry one
#: (`core.pager`, `core.sshCommand`, `diff.*.command`, `credential.helper`, `filter.*.clean`), a
#: pack program, or an exec path. Reproduced: `git -c alias.pwn='!python3 -c "..."' pwn` executed
#: the interpreter and wrote outside the writable roots while `allow_shell` was False.
_GIT_INDIRECTION = re.compile(r"(?:^|\s)(?:-c|--config-env)\s|--exec-path|--upload-pack|--receive-pack|\balias\.|\bcore\.(?:pager|sshCommand|editor|hooksPath)|\bcredential\.helper|\.command\s*=|\bfilter\.[^\s]*\.(?:clean|smudge)|(?:^|\s)git\s+(?:daemon|fast-import|filter-branch)\b", re.I)


def git_indirection(command: str) -> str:
    """Why this git invocation can run a program the argument list does not name, or ""."""
    if _GIT_INDIRECTION.search(_outside_single_quotes(command or "")) or _GIT_INDIRECTION.search(command or ""):
        return "a git configuration, alias, hook path or helper that can execute an arbitrary program"
    return ""


def unanalysable_command(command: str) -> str:
    """Why this command's effects cannot be read off its text, or "" when they can."""
    bare = _outside_single_quotes(command or "")
    if _SUBSTITUTION.search(bare):
        return "variable expansion or command substitution: the text does not name what it writes to or runs"
    if _INLINE_INTERPRETER.search(bare):
        return "an inline interpreter or command-building form: its effects are not in the command text"
    if _CHDIR.search(bare):
        return "an in-command directory change: relative paths in it do not resolve where they appear to"
    return ""


def _unquote(token: str) -> str:
    t = token.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        return t[1:-1]
    return t


def shell_write_targets(command: str) -> list[str]:
    """Best-effort extraction of paths a shell command would write to.

    Deliberately over-inclusive: a path that turns out to be harmless costs an extra
    classification, while a missed path costs a boundary. Options (tokens starting with `-`)
    and obvious non-paths are skipped.
    """
    targets: list[str] = []

    def add(raw: str) -> None:
        t = _unquote(raw)
        if not t or t.startswith("-") or t in ("&1", "&2") or t.startswith("$"):
            return
        if t not in targets:
            targets.append(t)

    for m in _REDIRECT_RE.finditer(command):
        add(m.group("target"))
    for m in _DD_OF_RE.finditer(command):
        add(m.group("target"))
    for m in _SED_INPLACE_RE.finditer(command):
        add(m.group("target"))
    for m in _WRITER_RE.finditer(command):
        for token in m.group("rest").split():
            add(token)
    return targets


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
        from cogos.tools.fabric import normalise_arguments

        args = normalise_arguments(call.tool, dict(call.arguments))
        # The `tests` substrate runs its command string through a shell exactly as `shell` does,
        # so it is classified identically. Without this it was an unclassified execution
        # primitive: the same `rm -rf ... && curl ... > /etc/passwd` was DENIED as `shell` and
        # ALLOWED as `run_tests`, because an unmatched substrate falls through to the spec's
        # default class. Intent ("this is verification") is not a capability.
        if spec.substrate in ("shell", "tests"):
            argv = args.get("argv")
            if isinstance(argv, list) and argv:
                # A structured invocation runs without a shell, so there is nothing to expand or
                # substitute and the unanalysable-form rule does not apply. The severity rules
                # still do: an argv that names a destructive or credential-sensitive operation is
                # the same operation whichever way it is spelled.
                joined = " ".join(str(a) for a in argv)
                base = classify_shell_command(joined)
                if base == ActionClass.REVERSIBLE_LOCAL and self._writes_outside_roots(joined, args.get("cwd")):
                    return ActionClass.CONSEQUENTIAL_SHARED
                return base
            command = str(args.get("command", ""))
            base = classify_shell_command(command)
            # A destination denied to write_file must be denied on this route too, so the shell
            # command's own write targets are classified by the same rule. A relative target
            # resolves against the call's own `cwd`, not the repo root: a caller that points cwd
            # outside the writable roots and writes to a bare filename is writing outside them.
            if base == ActionClass.REVERSIBLE_LOCAL and (unanalysable_command(command) or self._writes_outside_roots(command, args.get("cwd"))):
                return ActionClass.CONSEQUENTIAL_SHARED
            return base
        if spec.substrate == "git":
            argv = args.get("args") or []
            joined = "git " + (" ".join(map(str, argv)) if isinstance(argv, list) else str(argv))
            base = classify_shell_command(joined)
            if base == ActionClass.REVERSIBLE_LOCAL and (unanalysable_command(joined) or git_indirection(joined) or self._writes_outside_roots(joined)):
                return ActionClass.CONSEQUENTIAL_SHARED
            return base
        if spec.substrate == "filesystem":
            target = Path(str(args.get("path", "")))
            if not target.is_absolute():
                target = self.repo_root / target
            # Credential sensitivity is a property of the file, not of the verb. `cat .env` through
            # the shell requires human authorization, so reading the same path through `read_file`
            # must too — otherwise the boundary is decided by which tool the caller happened to
            # pick. This applies to reads and writes alike; the write-specific rules follow.
            if _CREDENTIAL.search(str(target)):
                return ActionClass.CREDENTIAL_SENSITIVE
            if spec.name not in ("write_file", "delete_file", "append_file"):
                return spec.default_action_class
            if spec.name == "delete_file":
                return ActionClass.DESTRUCTIVE if not _path_within(target, self.writable_roots) else ActionClass.REVERSIBLE_LOCAL
            return ActionClass.REVERSIBLE_LOCAL if _path_within(target, self.writable_roots) else ActionClass.CONSEQUENTIAL_SHARED
        if spec.substrate == "web":
            method = str(args.get("method", "GET")).upper()
            if method != "GET":
                return ActionClass.CONSEQUENTIAL_SHARED
            return ActionClass.REVERSIBLE_EXTERNAL
        return spec.default_action_class

    def _writes_outside_roots(self, command: str, cwd: Any = None) -> bool:
        base = self.repo_root
        if cwd:
            candidate = Path(str(cwd))
            base = candidate if candidate.is_absolute() else (self.repo_root / candidate)
            if not _path_within(base, self.writable_roots):
                # The working directory itself is outside the roots, so any relative write this
                # command performs lands outside them.
                if shell_write_targets(command):
                    return True
        for raw in shell_write_targets(command):
            target = Path(raw)
            if not target.is_absolute():
                target = base / target
            if not _path_within(target, self.writable_roots):
                return True
        return False

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
        if spec.substrate in ("shell", "tests", "git") and not cfg.allow_shell:
            # Every substrate that spawns a process, not just the one named "shell". `tests` runs
            # a command; `git` is an ungated process spawner in its own right — `git -c
            # alias.x='!<command>' x`, hooks, `core.pager`, `core.sshCommand`, `--upload-pack`
            # and credential helpers all execute arbitrary programs. Reproduced: with
            # `allow_shell=False`, a git alias still ran an interpreter and wrote outside the
            # writable roots. A policy that disables command execution has to disable it.
            return FirewallVerdict(decision=PolicyDecision.DENY, action_class=action_class, reason="process execution disabled by policy")
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
        # Writes outside writable roots are consequential/shared: deny unless granted. This
        # covers every execution route the firewall can classify, not just the filesystem tools —
        # a denied destination reached through a shell is the same denied destination.
        # `tests` belongs here for the same reason `shell` does: it spawns a shell, so a denied
        # destination reached through the test runner is the same denied destination. Classifying
        # it without enforcing it left the bypass open — the classification said
        # consequential_shared and this clause then allowed it.
        # Refused before the generic clause below so the reason names the actual problem: not
        # "this writes outside the roots" (which is unknown) but "what this does cannot be read
        # off the text". A human grant of `consequential_shared` still authorizes it, because the
        # judgement a person makes about an opaque command is exactly the judgement this cannot.
        if spec.substrate in ("shell", "git", "tests") and "consequential_shared" not in self.human_grants:
            from cogos.tools.fabric import normalise_arguments as _norm

            _args = _norm(call.tool, dict(call.arguments))
            if spec.substrate == "git":
                _argv = _args.get("args") or []
                _text = "git " + (" ".join(map(str, _argv)) if isinstance(_argv, list) else str(_argv))
            elif isinstance(_args.get("argv"), list) and _args.get("argv"):
                _text = ""  # no shell: nothing to expand, nothing to analyse around
            else:
                _text = str(_args.get("command", ""))
            why = unanalysable_command(_text) or (git_indirection(_text) if spec.substrate == "git" else "")
            if why:
                return FirewallVerdict(decision=PolicyDecision.DENY, action_class=action_class, reason=f"command cannot be analysed before execution: {why}")
        if spec.substrate in ("filesystem", "shell", "git", "tests") and action_class == ActionClass.CONSEQUENTIAL_SHARED and "consequential_shared" not in self.human_grants:
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
