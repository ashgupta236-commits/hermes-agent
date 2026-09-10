"""Skill discovery and progressive loading for prompts."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from cogos.skills.compiler import SkillDoc, _STOP

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")


def parse_skill_md(path: Path) -> Optional[SkillDoc]:
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    meta: dict[str, str] = {}
    body = text
    m = _FRONTMATTER_RE.match(text)
    if m:
        body = text[m.end():]
        for line in m.group(1).splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            meta[key.strip().lower()] = value
    name = meta.get("name") or path.parent.name
    description = meta.get("description", "")
    sections: dict[str, list[str]] = {}
    current = ""
    for line in body.splitlines():
        h = _HEADING_RE.match(line)
        if h:
            current = h.group(1).strip().lower()
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    procedure: list[str] = []
    for key, lines in sections.items():
        if "procedure" in key:
            for line in lines:
                b = _BULLET_RE.match(line)
                if b:
                    procedure.append(b.group(1).strip())
    triggers: list[str] = []
    for key, lines in sections.items():
        if "when to use" in key:
            for line in lines:
                if line.lower().startswith("triggers:"):
                    triggers = [t.strip() for t in line.split(":", 1)[1].split(",") if t.strip() and t.strip() != "(none)"]
    if not description:
        for key, lines in sections.items():
            if "when to use" in key:
                description = " ".join(x.strip() for x in lines if x.strip() and not x.lower().startswith("triggers:"))
                break
    try:
        version = int(meta.get("version", "1"))
    except ValueError:
        version = 1
    return SkillDoc(
        name=name,
        description=description,
        procedure=procedure,
        triggers=triggers,
        status="promoted",
        version=version,
        path=str(path),
    )


def discover(skills_dirs: list[Path]) -> list[SkillDoc]:
    docs: list[SkillDoc] = []
    seen: set[str] = set()
    for root in skills_dirs:
        root = Path(root)
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*/SKILL.md")):
            doc = parse_skill_md(path)
            if doc is None or doc.name in seen:
                continue
            seen.add(doc.name)
            docs.append(doc)
    return docs


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]{3,}", (text or "").lower()) if t not in _STOP}


def relevant_skills(objective: str, skills: list[SkillDoc], limit: int = 3, load_full: bool = False) -> list[SkillDoc]:
    """Rank skills by token overlap with the objective; return descriptions only unless ``load_full``."""
    obj = _tokens(objective)
    if not obj:
        return []
    scored: list[tuple[float, int, SkillDoc]] = []
    for idx, skill in enumerate(skills):
        name_t = _tokens(skill.name.replace("-", " "))
        desc_t = _tokens(skill.description)
        trig_t = _tokens(" ".join(skill.triggers))
        score = 2.0 * len(obj & name_t) + 1.0 * len(obj & desc_t) + 1.5 * len(obj & trig_t)
        if score > 0:
            scored.append((score, idx, skill))
    scored.sort(key=lambda x: (-x[0], x[1]))
    out: list[SkillDoc] = []
    for _, _, skill in scored[:limit]:
        if load_full:
            out.append(skill)
        else:
            out.append(skill.model_copy(update={"procedure": [], "evaluation": {}}))
    return out


def render_for_prompt(skills: list[SkillDoc], full: bool = False) -> str:
    if not skills:
        return ""
    lines = ["Available skills (learned procedures; follow only when they fit the objective):"]
    for s in skills:
        lines.append(f"- {s.name}: {s.description}")
        if full and s.procedure:
            for i, step in enumerate(s.procedure, start=1):
                lines.append(f"    {i}. {step}")
            if s.path:
                lines.append(f"    (source: {s.path})")
        elif s.path:
            lines.append(f"    (load full procedure from {s.path})")
    return "\n".join(lines)


__all__ = ["discover", "parse_skill_md", "relevant_skills", "render_for_prompt"]
