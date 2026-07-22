#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PYTHON_BIN="$BUNDLE_ROOT/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  printf 'Dev Loop runtime is missing or damaged. Rerun install/devloop.sh to repair it.\n' >&2
  exit 1
fi
export PYTHONPATH="$BUNDLE_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

exec "$PYTHON_BIN" -m devloop "$@"


