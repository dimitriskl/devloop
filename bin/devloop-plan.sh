#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PYTHON_BIN="$BUNDLE_ROOT/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  DEVELOPMENT_SETUP="$BUNDLE_ROOT/install/setup-development.sh"
  if [ ! -f "$DEVELOPMENT_SETUP" ]; then
    printf 'Dev Loop runtime and bootstrap script are missing from %s\n' "$BUNDLE_ROOT" >&2
    exit 1
  fi
  printf 'Dev Loop runtime not found; preparing the checkout-local runtime.\n'
  bash "$DEVELOPMENT_SETUP"
fi
if [ ! -x "$PYTHON_BIN" ]; then
  printf 'Dev Loop could not prepare its checkout-local runtime.\n' >&2
  exit 1
fi
export PYTHONPATH="$BUNDLE_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
if [[ -t 0 && -t 1 ]]; then
  export DEVLOOP_UI_MODE=application
else
  export DEVLOOP_UI_MODE=plain
fi

exec "$PYTHON_BIN" -m devloop.interactive_runner "$@"
