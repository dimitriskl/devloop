#!/usr/bin/env sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
release_temp="${TMPDIR:-/tmp}/devloop-codexcli-release-$$"
export UV_CACHE_DIR="$release_temp/uv-cache"
base_temp="$release_temp/pytest"

for required_command in uv pipx codex; do
  if ! command -v "$required_command" >/dev/null 2>&1; then
    printf '%s\n' "Required release command is unavailable: $required_command" >&2
    exit 1
  fi
done

if [ "${1:-}" = "--real-backend" ]; then
  codex login status
fi

uv sync --locked
uv run ruff check --no-cache .
uv run mypy --cache-dir "$release_temp/mypy"
uv run pytest -q -m 'not integration' --basetemp="$base_temp"
rm -f dist/devloop_codexcli-*.whl dist/devloop_codexcli-*.tar.gz
uv build --sdist --wheel --out-dir dist
uv run python install/verify-release.py --dist dist
wheel=$(find dist -maxdepth 1 -type f -name 'devloop_codexcli-0.1.0-*.whl' -print)
uv tool install --force "$wheel"
codexcli --help
codexcli doctor --help
codexcli run --help
uv tool uninstall devloop-codexcli
pipx install --force "$wheel"
pipx runpip devloop-codexcli show devloop-codexcli
codexcli doctor --help
codexcli run --help

if [ "${1:-}" = "--real-backend" ]; then
  export DEVLOOP_REAL_APP_SERVER=1
  export DEVLOOP_REAL_ANALYSIS=1
  export DEVLOOP_REAL_DEVELOPMENT=1
  export DEVLOOP_REAL_REVIEW_QA=1
  export DEVLOOP_REAL_REWORK=1
  export DEVLOOP_REAL_SCHEDULER=1
  export DEVLOOP_REAL_RECOVERY=1
  export DEVLOOP_REAL_UI=1
  codexcli doctor --repo "$root"
  uv run pytest -q -m integration --basetemp="$release_temp/pytest-real"
fi

printf '%s\n' 'PASS v0.1.0 release gates'
