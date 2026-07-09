#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

show_devloop_logo() {
  local logo_path="$BUNDLE_ROOT/docs/devloop-logo.txt"
  if [[ -f "$logo_path" ]]; then
    cat "$logo_path"
    printf '\n'
  fi
}

find_python() {
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      "$candidate" --version >/dev/null 2>&1 && {
        printf '%s\n' "$candidate"
        return 0
      }
    fi
  done

  printf 'Python 3.10+ was not found on PATH. Install Python and rerun this script.\n' >&2
  return 1
}

show_devloop_logo

PYTHON_BIN="$(find_python)"
export PYTHONPATH="$BUNDLE_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

exec "$PYTHON_BIN" -m devloop.interactive_runner "$@"
