#!/usr/bin/env bash
# SessionStart hook: print the cogos boot/recovery report so a fresh context recovers state
# without asking the human. Never fails the session: exits 0 on any problem.
set -u
ROOT="${CLAUDE_PROJECT_DIR:-$(pwd)}"
cd "$ROOT" 2>/dev/null || exit 0
HOME_DIR="${COGOS_HOME:-$ROOT/.cogos}"
[ -f "$HOME_DIR/cogos.db" ] || exit 0
PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3 || true)"
[ -n "$PY" ] || exit 0
echo "=== cogos boot report (durable mission state; see CLAUDE.md §4) ==="
timeout 60 "$PY" -m cogos boot --brief 2>/dev/null || echo "cogos boot failed; run '$PY -m cogos boot' manually"
exit 0
