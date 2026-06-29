#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

CODEX_SKILLS_PATH="${CODEX_SKILLS_PATH:-$HOME/.codex/skills}"
CODEX_AGENTS_PATH="${CODEX_AGENTS_PATH:-$HOME/.codex/agents}"

mkdir -p "$CODEX_SKILLS_PATH" "$CODEX_AGENTS_PATH"
cp -R "$BUNDLE_ROOT/skills/codex/"* "$CODEX_SKILLS_PATH/"
cp "$BUNDLE_ROOT/agents/codex/"*.md "$CODEX_AGENTS_PATH/"

printf 'Installed Codex skills to %s\n' "$CODEX_SKILLS_PATH"
printf 'Installed Codex agent references to %s\n' "$CODEX_AGENTS_PATH"


