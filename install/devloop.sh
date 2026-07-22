#!/usr/bin/env bash
# Install or update the portable Dev Loop bundle.
#
# Quick install:
#   curl -fsSL https://raw.githubusercontent.com/dimitriskl/devloop/main/install/devloop.sh | bash
#
# Update an existing install:
#   curl -fsSL https://raw.githubusercontent.com/dimitriskl/devloop/main/install/devloop.sh | bash
#
# Environment overrides:
#   DEVLOOP_INSTALL_DIR  bundle location (skips prompt when set)
#   DEVLOOP_BIN_DIR      command directory (default: ~/.local/bin)
#   DEVLOOP_REPO_URL     git clone URL (default: https://github.com/dimitriskl/devloop.git)
#   DEVLOOP_REF          branch or tag (default: main)

set -euo pipefail

DEFAULT_REPO_URL="https://github.com/dimitriskl/devloop.git"
DEFAULT_REF="main"
DEFAULT_INSTALL_DIR="$HOME/devloop"

INSTALL_DIR=""
INSTALL_DIR_SET=0
BIN_DIR="${DEVLOOP_BIN_DIR:-$HOME/.local/bin}"
REPO_URL="${DEVLOOP_REPO_URL:-$DEFAULT_REPO_URL}"
REF="${DEVLOOP_REF:-$DEFAULT_REF}"
INSTALL_SKILLS=1
LINK_COMMANDS=1

usage() {
  cat <<'EOF'
Usage: devloop.sh [options]

Install or update the portable Dev Loop bundle.

Options:
  --dir PATH       Install directory (skips prompt; default: ~/devloop)
  --bin-dir PATH   Directory for devloop commands (default: ~/.local/bin)
  --ref REF        Git branch or tag (default: main)
  --repo URL       Git repository URL
  --no-skills      Skip copying bundled Codex skills and agents
  --no-bin-links   Skip creating devloop command links
  -h, --help       Show this help

Environment:
  DEVLOOP_INSTALL_DIR, DEVLOOP_BIN_DIR, DEVLOOP_REPO_URL, DEVLOOP_REF

Examples:
  curl -fsSL https://raw.githubusercontent.com/dimitriskl/devloop/main/install/devloop.sh | bash
  ./install/devloop.sh --ref main
EOF
}

log() {
  printf 'devloop-install: %s\n' "$*"
}

die() {
  printf 'devloop-install: error: %s\n' "$*" >&2
  exit 1
}

require_command() {
  local name="$1"
  local hint="$2"
  if ! command -v "$name" >/dev/null 2>&1; then
    die "$hint"
  fi
}

find_python() {
  local candidate
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
        printf '%s\n' "$candidate"
        return 0
      fi
    fi
  done
  die "Python 3.10+ is required. Install Python and rerun this installer."
}

path_contains_dir() {
  local dir="$1"
  local entry
  IFS=':' read -r -a path_entries <<< "${PATH:-}"
  for entry in "${path_entries[@]}"; do
    if [ "$entry" = "$dir" ]; then
      return 0
    fi
  done
  return 1
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --dir)
        [ "$#" -ge 2 ] || die "--dir requires a path"
        INSTALL_DIR="$2"
        INSTALL_DIR_SET=1
        shift 2
        ;;
      --bin-dir)
        [ "$#" -ge 2 ] || die "--bin-dir requires a path"
        BIN_DIR="$2"
        shift 2
        ;;
      --ref)
        [ "$#" -ge 2 ] || die "--ref requires a value"
        REF="$2"
        shift 2
        ;;
      --repo)
        [ "$#" -ge 2 ] || die "--repo requires a URL"
        REPO_URL="$2"
        shift 2
        ;;
      --no-skills)
        INSTALL_SKILLS=0
        shift
        ;;
      --no-bin-links)
        LINK_COMMANDS=0
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "Unknown option: $1 (try --help)"
        ;;
    esac
  done
}

expand_home() {
  case "$1" in
    "~")
      printf '%s\n' "$HOME"
      ;;
    "~/"*)
      printf '%s/%s\n' "$HOME" "${1#~/}"
      ;;
    *)
      printf '%s\n' "$1"
      ;;
  esac
}

prompt_install_dir() {
  if [ "$INSTALL_DIR_SET" -eq 1 ]; then
    return 0
  fi

  if [ -n "${DEVLOOP_INSTALL_DIR:-}" ]; then
    INSTALL_DIR="$DEVLOOP_INSTALL_DIR"
    return 0
  fi

  local default="$DEFAULT_INSTALL_DIR"
  local reply=""
  if [ -t 0 ]; then
    printf 'Install directory [%s]: ' "$default"
    read -r reply
  elif [ -r /dev/tty ] 2>/dev/null; then
    if printf 'Install directory [%s]: ' "$default" >/dev/tty 2>/dev/null && read -r reply </dev/tty 2>/dev/null; then
      :
    else
      die "Install directory is required in non-interactive mode. Use --dir or DEVLOOP_INSTALL_DIR."
    fi
  else
    die "Install directory is required in non-interactive mode. Use --dir or DEVLOOP_INSTALL_DIR."
  fi

  if [ -n "$reply" ]; then
    INSTALL_DIR="$(expand_home "$reply")"
  else
    INSTALL_DIR="$default"
  fi
}

sync_bundle() {
  if [ -d "$INSTALL_DIR/.git" ]; then
    log "Updating existing install at $INSTALL_DIR"
    git -C "$INSTALL_DIR" fetch --depth 1 origin "$REF"
    git -C "$INSTALL_DIR" reset --hard FETCH_HEAD
    git -C "$INSTALL_DIR" clean -fd
    return 0
  fi

  if [ -e "$INSTALL_DIR" ]; then
    die "Install directory exists but is not a git checkout: $INSTALL_DIR"
  fi

  log "Installing Dev Loop to $INSTALL_DIR"
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone --depth 1 --branch "$REF" "$REPO_URL" "$INSTALL_DIR" 2>/dev/null || {
    rm -rf "$INSTALL_DIR"
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
    git -C "$INSTALL_DIR" checkout -f "$REF"
  }
}

make_scripts_executable() {
  chmod +x "$INSTALL_DIR/bin/devloop.sh" "$INSTALL_DIR/bin/devloop-plan.sh"
  if [ -d "$INSTALL_DIR/install" ]; then
    find "$INSTALL_DIR/install" -maxdepth 1 -name '*.sh' -exec chmod +x {} +
  fi
}

install_skills() {
  if [ "$INSTALL_SKILLS" -eq 0 ]; then
    return 0
  fi
  log "Installing bundled Codex skills and agent references"
  "$INSTALL_DIR/install/install-skills.sh"
}

install_portable_runtime() {
  if [ "${DEVLOOP_TESTING:-0}" = "1" ]; then
    return 0
  fi

  local base_python runtime next previous lock next_python
  base_python="$(find_python)"
  runtime="$INSTALL_DIR/.venv"
  next="$INSTALL_DIR/.venv.next"
  previous="$INSTALL_DIR/.venv.previous"
  lock="$INSTALL_DIR/requirements-portable.lock"
  next_python="$next/bin/python"

  log "Preparing isolated portable terminal runtime"
  rm -rf "$next"
  "$base_python" -m venv "$next"
  "$next_python" -m pip install --disable-pip-version-check --requirement "$lock"
  "$next_python" -c 'import textual; raise SystemExit(0 if textual.__version__ == "8.2.8" else 1)'

  rm -rf "$previous"
  if [ -d "$runtime" ]; then
    mv "$runtime" "$previous"
  fi
  if ! mv "$next" "$runtime"; then
    if [ -d "$previous" ]; then
      mv "$previous" "$runtime"
    fi
    die "could not activate the replacement portable runtime"
  fi
  rm -rf "$previous"
}

link_command() {
  local name="$1"
  local target="$2"
  mkdir -p "$BIN_DIR"
  ln -sf "$target" "$BIN_DIR/$name"
}

link_commands() {
  if [ "$LINK_COMMANDS" -eq 0 ]; then
    return 0
  fi
  log "Linking commands into $BIN_DIR"
  link_command devloop "$INSTALL_DIR/bin/devloop.sh"
  link_command devloop-plan "$INSTALL_DIR/bin/devloop-plan.sh"
}

print_next_steps() {
  local python
  if [ "${DEVLOOP_TESTING:-0}" = "1" ]; then
    python="$(find_python)"
  else
    python="$INSTALL_DIR/.venv/bin/python"
  fi

  cat <<EOF

Dev Loop is installed at:
  $INSTALL_DIR

Commands:
  devloop --help
  devloop-plan --help

EOF

  if [ "$LINK_COMMANDS" -eq 1 ] && ! path_contains_dir "$BIN_DIR"; then
    cat <<EOF
Add this directory to your PATH:
  export PATH="$BIN_DIR:\$PATH"

EOF
  fi

  if ! command -v codex >/dev/null 2>&1; then
    cat <<EOF
Codex CLI was not found on PATH. Install and authenticate Codex before running Dev Loop:
  codex --version
  codex login

EOF
  fi

  cat <<EOF
Optional isolated CodexCLI install from the bundle checkout:
  cd "$INSTALL_DIR" && uv tool install .

Verified isolated runtime:
  $("$python" --version)

Uninstall installer-managed artifacts while preserving source and project data:
  "$INSTALL_DIR/install/uninstall-devloop.sh" --dir "$INSTALL_DIR"
EOF
}

main() {
  parse_args "$@"
  prompt_install_dir
  require_command git "Git is required. Install Git and rerun this installer."
  find_python >/dev/null
  sync_bundle
  make_scripts_executable
  install_portable_runtime
  install_skills
  link_commands
  print_next_steps
}

main "$@"
