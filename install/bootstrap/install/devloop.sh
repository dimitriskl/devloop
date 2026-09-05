#!/usr/bin/env bash
set -euo pipefail
INSTALL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
exec bash "$INSTALL_ROOT/bootstrap/dispatch.sh" update "$@"
