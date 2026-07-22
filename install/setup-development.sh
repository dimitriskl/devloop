#!/usr/bin/env bash
# Prepare this development checkout without installing global commands or skills.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

usage() {
  cat <<'EOF'
Usage: setup-development.sh [--help]

Prepare this development checkout with its isolated Python runtime. This command
does not create shortcuts, change PATH, copy global skills, or update Git.
EOF
}

find_python() {
  local candidate
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
      "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  printf 'devloop-development: Python 3.10+ is required.\n' >&2
  exit 1
}

if [ "$#" -gt 0 ]; then
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'devloop-development: unknown option: %s\n' "$1" >&2
      exit 1
      ;;
  esac
fi

base_python="$(find_python)"
runtime="$BUNDLE_ROOT/.venv"
next="$BUNDLE_ROOT/.venv.next"
previous="$BUNDLE_ROOT/.venv.previous"
lock="$BUNDLE_ROOT/requirements-portable.lock"
next_python="$next/bin/python"

rm -rf -- "$next"
printf 'Preparing checkout-local runtime at %s\n' "$runtime"
"$base_python" -m venv "$next"
"$next_python" -m pip install --disable-pip-version-check --requirement "$lock"
"$next_python" -c 'import textual; raise SystemExit(0 if textual.__version__ == "8.2.8" else 1)'

rm -rf -- "$previous"
if [ -d "$runtime" ]; then
  mv "$runtime" "$previous"
fi
if ! mv "$next" "$runtime"; then
  if [ -d "$previous" ]; then
    mv "$previous" "$runtime"
  fi
  printf 'devloop-development: could not activate the replacement runtime.\n' >&2
  exit 1
fi
rm -rf -- "$previous"

printf '\nDevelopment runtime ready. No global installation changes were made.\n'
printf 'Run: %s/bin/devloop-plan.sh\n' "$BUNDLE_ROOT"
