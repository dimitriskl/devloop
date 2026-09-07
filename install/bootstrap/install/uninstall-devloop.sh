#!/usr/bin/env bash
set -euo pipefail
die() { printf 'devloop-uninstall: error: %s\n' "$*" >&2; exit 1; }
usage() {
    printf 'Usage: uninstall-devloop.sh [--dir PATH] [--keep-skills]\n'
    printf 'Interrupted uninstall resumes its original bound plan; incompatible retry options fail before mutation.\n'
}
parse_retry_options() {
    RETRY_ARGS=()
    REQUESTED_INSTALL_ROOT=''
    local help=0
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --dir|--bin-dir)
                [ "$#" -ge 2 ] && [ -n "$2" ] && [[ "$2" != --* ]] || die "$1 requires a path"
                if [ "$1" = --dir ]; then
                    REQUESTED_INSTALL_ROOT="$2"
                    RETRY_ARGS+=(--install-root "$2")
                else RETRY_ARGS+=(--bin-directory "$2"); fi
                shift 2 ;;
            --keep-skills) RETRY_ARGS+=(--keep-capabilities); shift ;;
            -h|--help) help=1; shift ;;
            *) die "unknown option: $1" ;;
        esac
    done
    if [ "$help" -eq 1 ]; then usage; exit 0; fi
    if [ "${CODEX_SKILLS_PATH+x}" ]; then
        [ -n "$CODEX_SKILLS_PATH" ] || die 'CODEX_SKILLS_PATH requires a path'
        RETRY_ARGS+=(--skills-destination "$CODEX_SKILLS_PATH")
    fi
    if [ "${CODEX_AGENTS_PATH+x}" ]; then
        [ -n "$CODEX_AGENTS_PATH" ] || die 'CODEX_AGENTS_PATH requires a path'
        RETRY_ARGS+=(--agents-destination "$CODEX_AGENTS_PATH")
    fi
}
parse_retry_options "$@"
SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
find_python() { for p in python3 python; do command -v "$p" >/dev/null 2>&1 && "$p" -B -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1 && { printf '%s\n' "$p"; return; }; done; return 1; }
PYTHON="$(find_python)" || { printf 'devloop-uninstall: error: Python 3.10+ is required\n' >&2; exit 1; }
RECOVERY_ROOT="$("$PYTHON" -B - "$SCRIPT_ROOT" <<'PY'
import hashlib, json, pathlib, re, stat, sys
base = pathlib.Path(sys.argv[1]).absolute()
if (base / 'journal.json').is_file():
    staging = base
else:
    install = base.parent
    lock = install.parent / ('.' + install.name + '.install-lock') / 'owner.json'
    if not lock.is_file():
        raise SystemExit(0)
    owner = json.loads(lock.read_text(encoding='utf-8'))
    if owner.get('operation') != 'uninstall':
        raise SystemExit(0)
    identity = owner.get('transaction_id', '')
    if re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}', identity) is None:
        raise RuntimeError('uninstall recovery lock identity is invalid')
    staging = install.parent / ('.' + install.name + '.uninstall-' + identity)
    if not (staging / 'journal.json').is_file():
        raise SystemExit(0)
journal = json.loads((staging / 'journal.json').read_text(encoding='utf-8'))
install = pathlib.Path(journal['install_root'])
if not install.is_absolute() or staging != install.parent / ('.' + install.name + '.uninstall-' + journal['transaction_id']):
    raise RuntimeError('uninstall recovery path is not owned')
evidence = json.loads((staging / 'action-evidence.json').read_text(encoding='utf-8'))
if any(evidence[key] != journal[key] for key in ('install_root', 'transaction_id', 'plan_hash', 'layout_hash')):
    raise RuntimeError('uninstall recovery evidence mismatch')
for name in ('transaction.py', 'verify.py'):
    path = staging / name
    for ancestor in (path, *path.parents):
        status = ancestor.lstat()
        if stat.S_ISLNK(status.st_mode) or getattr(status, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise RuntimeError('uninstall recovery rejects linked paths')
    expected = evidence['layout']['assets']['bootstrap/' + name]
    if hashlib.sha256(path.read_bytes()).hexdigest().upper() != expected:
        raise RuntimeError('uninstall recovery executable was modified')
print(staging)
PY
)"
if [ -n "$RECOVERY_ROOT" ]; then
    exec "$PYTHON" -B "$RECOVERY_ROOT/transaction.py" resume-uninstall "$RECOVERY_ROOT" --owner-pid "$$" --protocol 2 "${RETRY_ARGS[@]}"
fi
INSTALL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [ -n "$REQUESTED_INSTALL_ROOT" ]; then
    [ "$(cd "$REQUESTED_INSTALL_ROOT" && pwd -P)" = "$INSTALL_ROOT" ] || die 'install directory differs from this installed launcher'
fi
exec bash "$INSTALL_ROOT/bootstrap/dispatch.sh" uninstall "$@"
