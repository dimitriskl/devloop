#!/usr/bin/env bash
# Remove artifacts created by the portable Dev Loop installer.
#
# The source checkout, project PRDs, worktrees, and branches are never removed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALL_DIR="${DEVLOOP_INSTALL_DIR:-$BUNDLE_ROOT}"
BIN_DIR="${DEVLOOP_BIN_DIR:-$HOME/.local/bin}"
CODEX_SKILLS_PATH="${CODEX_SKILLS_PATH:-$HOME/.codex/skills}"
CODEX_AGENTS_PATH="${CODEX_AGENTS_PATH:-$HOME/.codex/agents}"
KEEP_SKILLS=0

usage() {
  cat <<'EOF'
Usage: uninstall-devloop.sh [options]

Remove artifacts created by the portable Dev Loop installer while preserving
the source checkout, project PRDs, worktrees, and branches.

Options:
  --dir PATH          Bundle whose local runtime should be removed
  --bin-dir PATH      Command-link directory (default: ~/.local/bin)
  --keep-skills       Keep installed Codex skills and agent references
  -h, --help          Show this help
EOF
}

die() {
  printf 'devloop-uninstall: error: %s\n' "$*" >&2
  exit 1
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --dir)
        [ "$#" -ge 2 ] || die "--dir requires a path"
        INSTALL_DIR="$2"
        shift 2
        ;;
      --bin-dir)
        [ "$#" -ge 2 ] || die "--bin-dir requires a path"
        BIN_DIR="$2"
        shift 2
        ;;
      --keep-skills)
        KEEP_SKILLS=1
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

assert_safe_install_dir() {
  if [ -e "$INSTALL_DIR" ] && [ ! -d "$INSTALL_DIR" ]; then
    die "install directory is not a directory: $INSTALL_DIR"
  fi
  if [ -d "$INSTALL_DIR" ]; then
    INSTALL_DIR="$(cd "$INSTALL_DIR" && pwd -P)"
  fi
  case "$INSTALL_DIR" in
    ""|"/")
      die "refusing to use filesystem root as the install directory"
      ;;
  esac
}

remove_local_runtime() {
  local name path
  [ -d "$INSTALL_DIR" ] || return 0
  for name in .venv .venv.next .venv.previous; do
    path="$INSTALL_DIR/$name"
    if [ -e "$path" ]; then
      rm -rf -- "$path"
      printf 'Removed runtime artifact: %s\n' "$path"
    fi
  done
}

remove_managed_link() {
  local name="$1"
  local script_name="$2"
  local path="$BIN_DIR/$name"
  local target
  [ -L "$path" ] || return 0
  target="$(readlink "$path")"
  case "$target" in
    */bin/"$script_name")
      rm -- "$path"
      printf 'Removed command link: %s\n' "$path"
      ;;
  esac
}

remove_matching_files() {
  local source_root="$1"
  local destination_root="$2"
  local source_file source_directory relative destination
  [ -d "$source_root" ] || return 0
  while IFS= read -r -d '' source_file; do
    relative="${source_file#"$source_root"/}"
    destination="$destination_root/$relative"
    [ -f "$destination" ] || continue
    if cmp -s -- "$source_file" "$destination"; then
      rm -- "$destination"
      printf 'Removed installed capability: %s\n' "$destination"
    else
      printf 'Kept modified capability: %s\n' "$destination"
    fi
  done < <(find "$source_root" -type f -print0)
  while IFS= read -r -d '' source_directory; do
    [ "$source_directory" = "$source_root" ] && continue
    relative="${source_directory#"$source_root"/}"
    rmdir "$destination_root/$relative" 2>/dev/null || true
  done < <(find "$source_root" -depth -type d -print0)
}

parse_args "$@"
assert_safe_install_dir
remove_local_runtime
remove_managed_link devloop devloop.sh
remove_managed_link devloop-plan devloop-plan.sh
if [ -d "$BIN_DIR" ]; then
  rmdir "$BIN_DIR" 2>/dev/null || true
fi

if [ "$KEEP_SKILLS" -eq 0 ]; then
  remove_matching_files "$BUNDLE_ROOT/skills/codex" "$CODEX_SKILLS_PATH"
  remove_matching_files "$BUNDLE_ROOT/agents/codex" "$CODEX_AGENTS_PATH"
fi

printf '\nDev Loop installer artifacts were removed.\n'
printf 'Source checkout preserved: %s\n' "$INSTALL_DIR"
