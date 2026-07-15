#!/usr/bin/env sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
tier=${1:-}
artifact_option=${2:-}
case "$tier" in
  fast|vertical|release) ;;
  *) printf '%s\n' "Usage: $0 fast|vertical|release" >&2; exit 2 ;;
esac
if [ -n "$artifact_option" ] && [ "$artifact_option" != "--use-existing-artifacts" ]; then
  printf '%s\n' "Unknown verification option: $artifact_option" >&2
  exit 2
fi

platform=$(uname -s | tr '[:upper:]' '[:lower:]')
evidence_dir="$root/.release-evidence"
result_log="$evidence_dir/$platform-$tier.log"
manifest="$evidence_dir/$platform-$tier.json"

if [ "${DEVLOOP_GATE_CAPTURE:-0}" != "1" ]; then
  mkdir -p "$evidence_dir"
  started=$(date +%s)
  set +e
  DEVLOOP_GATE_CAPTURE=1 "$0" "$tier" ${artifact_option:+"$artifact_option"} >"$result_log" 2>&1
  status=$?
  set -e
  cat "$result_log"
  finished=$(date +%s)
  duration_ms=$(( (finished - started) * 1000 ))
  result=PASSED
  if [ "$status" -ne 0 ]; then result=FAILED; fi
  set --
  if [ "$tier" = "release" ]; then
    for artifact in dist/devloop_codexcli-0.1.0-*.whl dist/devloop_codexcli-0.1.0.tar.gz; do
      if [ -f "$artifact" ]; then set -- "$@" --artifact "$artifact"; fi
    done
  fi
  uv run codexcli-gate \
    --tier "$tier" \
    --repo "$root" \
    --output "$manifest" \
    --gate-id "$platform-$tier" \
    --status "$result" \
    --duration-ms "$duration_ms" \
    --result-log "$result_log" \
    "$@"
  exit "$status"
fi

release_temp="${TMPDIR:-/tmp}/devloop-codexcli-$tier-$$"
export UV_CACHE_DIR="$release_temp/uv-cache"

case "$tier" in
  fast)
    uv sync --locked
    uv run ruff check --no-cache .
    uv run mypy --cache-dir "$release_temp/mypy"
    uv run pytest -q -m 'not integration' --basetemp="$release_temp/pytest-fast"
    ;;
  vertical)
    codex login status
    export DEVLOOP_REAL_VERTICAL=1
    uv sync --locked
    uv run codexcli doctor --repo "$root"
    uv run pytest -q -m integration tests/codexcli/test_real_vertical_workflow.py \
      --basetemp="$release_temp/pytest-vertical"
    ;;
  release)
    ./install/run-release-gates.sh --real-backend ${artifact_option:+"$artifact_option"}
    ;;
esac
