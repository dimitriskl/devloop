#!/usr/bin/env bash
# Install or update Portable Dev Loop through immutable side-by-side releases.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
INSTALL_DIR="${DEVLOOP_INSTALL_DIR:-}"
REPO_URL="${DEVLOOP_REPO_URL:-https://github.com/dimitriskl/devloop.git}"
REF="${DEVLOOP_REF:-main}"
INSTALL_SKILLS=1
ROLLBACK=0
CANDIDATE_DIR=""
TRANSACTION_ID=""

usage() {
  cat <<'EOF'
Usage: devloop.sh [options]

  --dir PATH       Stable bootstrap directory (default: ~/devloop)
  --repo URL       Git repository URL
  --ref REF        Git branch, tag, or commit (default: main)
  --no-skills      Skip bundled Codex capabilities
  --rollback       Atomically select the previously current release
EOF
}

die() { printf 'devloop-install: error: %s\n' "$*" >&2; exit 1; }
log() { printf 'devloop-install: %s\n' "$*"; }

find_python() {
  local candidate
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --dir) [ "$#" -ge 2 ] || die '--dir requires a path'; INSTALL_DIR="$2"; shift 2 ;;
      --repo) [ "$#" -ge 2 ] || die '--repo requires a URL'; REPO_URL="$2"; shift 2 ;;
      --ref) [ "$#" -ge 2 ] || die '--ref requires a value'; REF="$2"; shift 2 ;;
      --no-skills) INSTALL_SKILLS=0; shift ;;
      --rollback) ROLLBACK=1; shift ;;
      --bin-dir|--no-bin-links) if [ "$1" = '--bin-dir' ]; then shift 2; else shift; fi ;;
      -h|--help) usage; exit 0 ;;
      *) die "unknown option: $1" ;;
    esac
  done
}

absolute_install_dir() {
  [ -n "$INSTALL_DIR" ] || INSTALL_DIR="$HOME/devloop"
  case "$INSTALL_DIR" in
    /*) ;;
    *) INSTALL_DIR="$PWD/$INSTALL_DIR" ;;
  esac
  INSTALL_DIR="${INSTALL_DIR%/}"
  [ -n "$INSTALL_DIR" ] && [ "$INSTALL_DIR" != / ] || die 'refusing filesystem root as InstallDir'
  mkdir -p "$(dirname "$INSTALL_DIR")"
  [ ! -L "$(dirname "$INSTALL_DIR")" ] || die 'install parent cannot be a symbolic link'
  [ ! -L "$INSTALL_DIR" ] || die 'InstallDir cannot be a symbolic link'
}

cleanup_candidate() {
  if [ -n "$CANDIDATE_DIR" ] && [ -d "$CANDIDATE_DIR" ]; then
    rm -rf -- "$CANDIDATE_DIR"
  fi
}

clone_candidate() {
  local leaf parent
  parent="$(dirname "$INSTALL_DIR")"
  leaf="$(basename "$INSTALL_DIR")"
  CANDIDATE_DIR="$parent/.${leaf}.candidate-$TRANSACTION_ID"
  mkdir "$CANDIDATE_DIR"
  if [ "${DEVLOOP_TESTING:-0}" = 1 ] && [ "${DEVLOOP_TEST_INTERRUPT_CANDIDATE_BOUNDARY:-}" = candidate_created ]; then exit 92; fi
  log "staging ref $REF outside the stable bootstrap"
  if ! git clone --depth 1 --branch "$REF" "$REPO_URL" "$CANDIDATE_DIR"; then
    rm -rf -- "$CANDIDATE_DIR"
    git clone --depth 1 "$REPO_URL" "$CANDIDATE_DIR"
    git -C "$CANDIDATE_DIR" checkout -f "$REF"
  fi
  CANDIDATE_COMMIT="$(git -C "$CANDIDATE_DIR" rev-parse HEAD)"
  case "$CANDIDATE_COMMIT" in
    *[!0-9a-f]*|'') die 'candidate commit is invalid' ;;
  esac
  [ "${#CANDIDATE_COMMIT}" -eq 40 ] || die 'candidate commit is invalid'
  git -C "$CANDIDATE_DIR" reset --hard "$CANDIDATE_COMMIT" >/dev/null
  git -C "$CANDIDATE_DIR" clean -ffdx >/dev/null
  if [ "${DEVLOOP_TESTING:-0}" = 1 ] && [ "${DEVLOOP_TEST_INTERRUPT_CANDIDATE_BOUNDARY:-}" = candidate_cloned ]; then exit 92; fi
}

validate_release_command() {
  local python="$1" root="$2" command="$3"
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$root/src" "$python" -m devloop.portable_release "$command"
}

begin_transaction() {
  local entry="$1" operation="$2" output
  output="$(mktemp "${TMPDIR:-/tmp}/devloop-transaction.XXXXXX")"
  if ! "$(find_python)" "$entry" begin "$INSTALL_DIR" "$operation" --protocol 2 > "$output"; then
    rm -f -- "$output"
    return 1
  fi
  IFS= read -r TRANSACTION_ID < "$output"
  TRANSACTION_ID="${TRANSACTION_ID%$'\r'}"
  rm -f -- "$output"
  [ -n "$TRANSACTION_ID" ] || die 'bootstrap returned no transaction identity'
}

begin_legacy_migration() {
  local entry="$1" output
  output="$(mktemp "${TMPDIR:-/tmp}/devloop-transaction.XXXXXX")"
  if ! "$(find_python)" "$entry" begin-legacy-migration "$INSTALL_DIR" --protocol 2 > "$output"; then
    rm -f -- "$output"
    return 1
  fi
  IFS= read -r TRANSACTION_ID < "$output"
  TRANSACTION_ID="${TRANSACTION_ID%$'\r'}"
  rm -f -- "$output"
  [ -n "$TRANSACTION_ID" ] || die 'bootstrap returned no transaction identity'
}

install_runtime() {
  local python runtime
  python="$(find_python)" || die 'Python 3.10+ is required'
  runtime="$CANDIDATE_DIR/.venv"
  if [ "${DEVLOOP_TESTING:-0}" = 1 ]; then
    validate_release_command "$python" "$CANDIDATE_DIR" validate-runtime
    mkdir "$runtime"
    printf '%s' "$CANDIDATE_COMMIT" > "$runtime/.devloop-test-runtime"
    return
  fi
  "$python" -m venv "$runtime"
  "$runtime/bin/python" -m pip install --disable-pip-version-check --requirement "$CANDIDATE_DIR/requirements-portable.lock"
  validate_release_command "$runtime/bin/python" "$CANDIDATE_DIR" validate-runtime
}

initialize_user_state() {
  local release="$1" python
  if [ "${DEVLOOP_TESTING:-0}" = 1 ]; then python="$(find_python)"; else python="$release/.venv/bin/python"; fi
  log 'initializing v3 user state and idempotent v0.2.1 adoption'
  validate_release_command "$python" "$release" prepare-user-state
}

install_capabilities() {
  local release="$1"
  [ "$INSTALL_SKILLS" -eq 1 ] || return 0
  if [ "${DEVLOOP_TESTING:-0}" = 1 ] && [ "${DEVLOOP_TEST_CAPABILITY_FAILURE:-0}" = 1 ]; then
    log 'capability installation warning: injected failure'
    return 0
  fi
  if ! "$release/install/install-skills.sh"; then
    log 'capability installation warning: bundled capabilities were not updated'
  fi
}

recover_transaction() {
  [ -f "$INSTALL_DIR/bootstrap/install-transaction.json" ] || return 1
  local python result action release
  python="$(find_python)" || die 'Python 3.10+ is required'
  result="$($python "$INSTALL_DIR/bootstrap/transaction.py" recover "$INSTALL_DIR" "$TRANSACTION_ID" --protocol 2)"
  action="${result%%$'\t'*}"
  release="${result#*$'\t'}"
  case "$action" in
    NEEDS_ADOPTION) initialize_user_state "$release"; "$python" "$INSTALL_DIR/bootstrap/transaction.py" commit "$INSTALL_DIR" "$TRANSACTION_ID" --protocol 2 ;;
    READY_TO_SWITCH) "$python" "$INSTALL_DIR/bootstrap/transaction.py" commit "$INSTALL_DIR" "$TRANSACTION_ID" --protocol 2 ;;
    COMPLETE) ;;
    *) die "unsupported recovery action: $action" ;;
  esac
  install_capabilities "$release"
  return 0
}

current_release() {
  local python
  python="$(find_python)" || die 'Python 3.10+ is required'
  "$python" "$INSTALL_DIR/bootstrap/verify.py" "$INSTALL_DIR"
}

main() {
  parse_args "$@"
  absolute_install_dir
  command -v git >/dev/null 2>&1 || die 'Git is required'
  find_python >/dev/null || die 'Python 3.10+ is required'
  if [ "$ROLLBACK" -eq 1 ]; then
    [ -f "$INSTALL_DIR/bootstrap/layout.json" ] || die 'stable bootstrap is not installed'
    begin_transaction "$INSTALL_DIR/bootstrap/transaction.py" rollback
    "$(find_python)" "$INSTALL_DIR/bootstrap/transaction.py" rollback "$INSTALL_DIR" "$TRANSACTION_ID" --protocol 2
    log "rolled back current release: $(current_release)"
    return 0
  fi
  local bootstrap_entry source_entry
  source_entry="$SCRIPT_DIR/bootstrap/transaction.py"
  if [ -f "$INSTALL_DIR/bootstrap/transaction.py" ]; then bootstrap_entry="$INSTALL_DIR/bootstrap/transaction.py"; else bootstrap_entry="$SCRIPT_DIR/bootstrap/transaction.py"; fi
  if ! begin_transaction "$bootstrap_entry" install; then
    [ "$bootstrap_entry" != "$source_entry" ] || die 'could not acquire install lock'
    bootstrap_entry="$source_entry"
    begin_legacy_migration "$bootstrap_entry" || die 'could not acquire compatible install lock'
  fi
  if [ -f "$INSTALL_DIR/bootstrap/layout.json" ] && recover_transaction; then
    log "recovered current release: $(current_release)"
    return 0
  fi
  trap cleanup_candidate EXIT
  clone_candidate
  install_runtime
  local python release
  python="$(find_python)"
  if [ -f "$INSTALL_DIR/bootstrap/current.json" ]; then
    local current_release current_commit
    current_release="$($python "$INSTALL_DIR/bootstrap/verify.py" "$INSTALL_DIR")"
    current_commit="$(git -C "$current_release" rev-parse HEAD)"
    if [ "$current_commit" = "$CANDIDATE_COMMIT" ]; then
      cleanup_candidate
      CANDIDATE_DIR=""
      initialize_user_state "$current_release"
      "$python" "$bootstrap_entry" abort "$INSTALL_DIR" "$TRANSACTION_ID" --protocol 2
      trap - EXIT
      install_capabilities "$current_release"
      log "current immutable release: $CANDIDATE_COMMIT"
      return 0
    fi
  fi
  "$python" "$CANDIDATE_DIR/install/bootstrap/transaction.py" publish "$INSTALL_DIR" "$CANDIDATE_DIR" "$TRANSACTION_ID" --protocol 2
  release="$($python "$INSTALL_DIR/bootstrap/transaction.py" prepare "$INSTALL_DIR" "$CANDIDATE_DIR" "$CANDIDATE_COMMIT" "$TRANSACTION_ID" --protocol 2)"
  CANDIDATE_DIR=""
  initialize_user_state "$release"
  "$python" "$INSTALL_DIR/bootstrap/transaction.py" commit "$INSTALL_DIR" "$TRANSACTION_ID" --protocol 2
  trap - EXIT
  install_capabilities "$release"
  log "current immutable release: $CANDIDATE_COMMIT"
  printf 'Run: %s/bin/devloop.sh --help\n' "$INSTALL_DIR"
}

main "$@"
