#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

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

PYTHON_BIN="$(find_python)"
export PYTHONPATH="$BUNDLE_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
"$PYTHON_BIN" -m devloop.logo "$BUNDLE_ROOT" || true

exec "$PYTHON_BIN" -m devloop.interactive_runner "$@"
