#!/usr/bin/env sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
DEVLOOP_REAL_RECOVERY=1 uv run pytest -q tests/codexcli/test_real_recovery.py \
  --basetemp=".tmp-pytest-release-recovery-$$"
