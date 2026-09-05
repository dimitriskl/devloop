#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ACTIVE_RELEASE="$(cd "$SCRIPT_DIR/.." && pwd -P)"
INSTALL_DIR="${DEVLOOP_INSTALL_DIR:-$(cd "$ACTIVE_RELEASE/../.." && pwd -P)}"
CODEX_SKILLS_PATH="${CODEX_SKILLS_PATH:-$HOME/.codex/skills}"
CODEX_AGENTS_PATH="${CODEX_AGENTS_PATH:-$HOME/.codex/agents}"
KEEP_SKILLS=0

die() { printf 'devloop-uninstall: error: %s\n' "$*" >&2; exit 1; }
find_python() { for p in python3 python; do command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1 && { printf '%s\n' "$p"; return; }; done; return 1; }
usage() { printf 'Usage: uninstall-devloop.sh [--dir PATH] [--keep-skills]\n'; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dir) [ "$#" -ge 2 ] || die '--dir requires a path'; INSTALL_DIR="$2"; shift 2 ;;
    --keep-skills) KEEP_SKILLS=1; shift ;;
    --bin-dir) shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

case "$INSTALL_DIR" in ''|/) die 'refusing filesystem root' ;; esac
[ -d "$INSTALL_DIR" ] || die 'InstallDir does not exist'
[ ! -L "$INSTALL_DIR" ] || die 'InstallDir cannot be a symbolic link'
[ -f "$INSTALL_DIR/bootstrap/transaction.py" ] || die 'stable layout manifest is missing; nothing was removed'
PYTHON="$(find_python)" || die 'Python 3.10+ is required to verify managed releases'
TRANSACTION_OUTPUT="$(mktemp "${TMPDIR:-/tmp}/devloop-uninstall.XXXXXX")"
$PYTHON "$INSTALL_DIR/bootstrap/transaction.py" begin "$INSTALL_DIR" uninstall --protocol 2 > "$TRANSACTION_OUTPUT"
IFS= read -r TRANSACTION_ID < "$TRANSACTION_OUTPUT"
TRANSACTION_ID="${TRANSACTION_ID%$'\r'}"
rm -f -- "$TRANSACTION_OUTPUT"
ARGS=("$INSTALL_DIR/bootstrap/transaction.py" uninstall "$INSTALL_DIR" "$TRANSACTION_ID" --protocol 2)
if [ "$KEEP_SKILLS" -eq 1 ]; then
  ARGS+=(--keep-capabilities)
else
  ARGS+=(--skills-destination "$CODEX_SKILLS_PATH" --agents-destination "$CODEX_AGENTS_PATH")
fi
"$PYTHON" "${ARGS[@]}"
printf 'Dev Loop managed bootstrap and immutable releases were removed.\n'
printf 'Portable catalog, project data, and legacy checkout content were preserved.\n'
