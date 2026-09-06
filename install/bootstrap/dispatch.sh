#!/usr/bin/env bash
set -euo pipefail
COMMAND="${1:?command is required}"
shift
INSTALL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
find_python() { for p in python3 python; do command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1 && { printf '%s\n' "$p"; return; }; done; return 1; }
PYTHON="$(find_python)" || { printf 'devloop-bootstrap: error: Python 3.10+ is required\n' >&2; exit 1; }
case "$COMMAND" in
  uninstall)
    TRANSACTION_OUTPUT="$(mktemp "${TMPDIR:-/tmp}/devloop-uninstall.XXXXXX")"
    "$PYTHON" -B "$INSTALL_ROOT/bootstrap/transaction.py" begin "$INSTALL_ROOT" uninstall --protocol 2 > "$TRANSACTION_OUTPUT"
    IFS= read -r TRANSACTION_ID < "$TRANSACTION_OUTPUT"
    TRANSACTION_ID="${TRANSACTION_ID%$'\r'}"
    rm -f -- "$TRANSACTION_OUTPUT"
    UNINSTALL_ARGS=("$INSTALL_ROOT/bootstrap/transaction.py" uninstall "$INSTALL_ROOT" "$TRANSACTION_ID" --protocol 2)
    KEEP_CAPABILITIES=0
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --dir|--bin-dir) shift 2 ;;
        --keep-skills) KEEP_CAPABILITIES=1; shift ;;
        *) printf 'devloop-bootstrap: error: unsupported uninstall option: %s\n' "$1" >&2; exit 1 ;;
      esac
    done
    if [ "$KEEP_CAPABILITIES" -eq 1 ]; then
      UNINSTALL_ARGS+=(--keep-capabilities)
    else
      UNINSTALL_ARGS+=(--skills-destination "${CODEX_SKILLS_PATH:-$HOME/.codex/skills}" --agents-destination "${CODEX_AGENTS_PATH:-$HOME/.codex/agents}")
    fi
    exec "$PYTHON" -B "${UNINSTALL_ARGS[@]}"
    ;;
esac
VERIFY_ARGS=("$INSTALL_ROOT/bootstrap/verify.py" "$INSTALL_ROOT")
[ "$COMMAND" != update ] || VERIFY_ARGS+=(--update-driver)
RELEASE_ROOT="$("$PYTHON" -B "${VERIFY_ARGS[@]}")"
case "$COMMAND" in
  devloop) TARGET="$RELEASE_ROOT/bin/devloop.sh" ;;
  devloop-plan) TARGET="$RELEASE_ROOT/bin/devloop-plan.sh" ;;
  update) TARGET="$RELEASE_ROOT/install/devloop.sh"; set -- --dir "$INSTALL_ROOT" "$@" ;;
  *) printf 'devloop-bootstrap: error: unsupported command: %s\n' "$COMMAND" >&2; exit 1 ;;
esac
[ -f "$TARGET" ] || { printf 'devloop-bootstrap: error: current command is missing: %s\n' "$TARGET" >&2; exit 1; }
# The policy belongs to this child, including immutable older wrappers without -B.
PYTHONDONTWRITEBYTECODE=1 exec bash "$TARGET" "$@"
