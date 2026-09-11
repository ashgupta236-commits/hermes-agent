---
name: cogos-skill-compiler
description: How cogos extracts reusable procedures from successful missions and evaluates candidate skills before promotion — use when proposing, evaluating, or promoting a skill.
---

# Skill compiler pipeline

successful trajectory → identify reusable procedure → remove mission-specific details
(`<path>`, `<url>`, `<n>`, `<topic>` placeholders) → candidate skill → generate evaluation cases
(normal + adversarial injection + missing tool + regression) → test against baseline →
promote only if: skill ≥ baseline + 0.05, all adversarial cases safe, all regression cases pass.

```bash
python -m cogos skills propose <mission_id>      # only for COMPLETE missions with ≥3 steps
python -m cogos skills evaluate <mission_id>     # deterministic procedural runner; promotes or rejects
python -m cogos skills list
```
Promoted skills are written to `.cogos/skills/<name>/SKILL.md` and mirrored to
`.claude/skills/<name>/SKILL.md` (Agent Skills convention: YAML frontmatter `name`/`description`,
progressive loading). A candidate is never trusted because it worked once. Self-improvement may
touch skills, prompts, heuristics, memory policies, agent templates, tool selection, verification
procedures and code — never permission boundaries, safety boundaries, auditability, provenance
requirements or human-authorization rules.
