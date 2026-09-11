#!/usr/bin/env bash
# PreCompact hook: export a snapshot of the most recent mission before context compaction.
set -u
ROOT="${CLAUDE_PROJECT_DIR:-$(pwd)}"
cd "$ROOT" 2>/dev/null || exit 0
HOME_DIR="${COGOS_HOME:-$ROOT/.cogos}"
[ -f "$HOME_DIR/cogos.db" ] || exit 0
PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3 || true)"
[ -n "$PY" ] || exit 0
timeout 60 "$PY" -m cogos checkpoint >/dev/null 2>&1 || true
echo "cogos: mission snapshot exported before compaction (resume with: python -m cogos boot)"
exit 0
