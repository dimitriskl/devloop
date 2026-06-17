#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

CODEX_SKILLS_PATH="${CODEX_SKILLS_PATH:-$HOME/.codex/skills}"
CLAUDE_AGENTS_PATH="${CLAUDE_AGENTS_PATH:-$HOME/.claude/agents}"

mkdir -p "$CODEX_SKILLS_PATH" "$CLAUDE_AGENTS_PATH"
cp -R "$BUNDLE_ROOT/skills/codex/"* "$CODEX_SKILLS_PATH/"
cp "$BUNDLE_ROOT/agents/claude/"*.md "$CLAUDE_AGENTS_PATH/"

printf 'Installed Codex skills to %s\n' "$CODEX_SKILLS_PATH"
printf 'Installed Claude agents to %s\n' "$CLAUDE_AGENTS_PATH"


