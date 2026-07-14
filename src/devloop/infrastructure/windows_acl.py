from __future__ import annotations

import csv
import io
import re
import subprocess
from functools import lru_cache
from pathlib import Path

_WINDOWS_SID = re.compile(r"S-1(?:-[0-9]+)+\Z")
_ICACLS_GRANT = re.compile(
    r'^icacls\s+(?:"(?P<quoted>[^"]+)"|(?P<plain>[^\s]+))\s+'
    r'/grant:r\s+"?\*(?P<sid>S-1(?:-[0-9]+)+):\(F\)"?$',
    re.IGNORECASE,
)
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_UNSAFE_ACL_TARGET_CHARACTERS = "$`()@*?[],"


class WindowsAclError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def current_windows_user_sid() -> str:
    try:
        completed = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise WindowsAclError("Unable to resolve the current Windows user SID.") from error
    try:
        row = next(csv.reader(io.StringIO(completed.stdout)))
    except (StopIteration, csv.Error) as error:
        raise WindowsAclError("Windows returned an invalid current-user identity.") from error
    if completed.returncode != 0 or len(row) < 2 or _WINDOWS_SID.fullmatch(row[1]) is None:
        raise WindowsAclError("Windows returned an invalid current-user SID.")
    return row[1]


def protect_current_windows_user_path(path: Path, *, directory: bool) -> None:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise WindowsAclError("The private Windows path is unavailable.") from error
    sid = current_windows_user_sid()
    grant = f"*{sid}:(OI)(CI)F" if directory else f"*{sid}:(F)"
    try:
        completed = subprocess.run(
            [
                "icacls",
                str(resolved),
                "/inheritance:r",
                "/grant:r",
                grant,
            ],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=10.0,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise WindowsAclError("Unable to protect private Windows storage.") from error
    if completed.returncode != 0:
        raise WindowsAclError("Unable to protect private Windows storage.")


def is_safe_windows_acl_grant(
    command: str,
    workspace: Path,
    sid: str,
) -> bool:
    if any(character in command for character in ";&|><\r\n"):
        return False
    match = _ICACLS_GRANT.fullmatch(command.strip())
    if match is None or match.group("sid").casefold() != sid.casefold():
        return False
    target_value = match.group("quoted") or match.group("plain")
    if target_value is None:
        return False
    if (
        target_value.startswith(("~", "\\\\?\\", "\\\\.\\"))
        or any(character in target_value for character in _UNSAFE_ACL_TARGET_CHARACTERS)
        or (
            ":" in target_value
            and (
                _WINDOWS_ABSOLUTE_PATH.match(target_value) is None
                or ":" in target_value[2:]
            )
        )
    ):
        return False
    root = workspace.resolve()
    target = Path(target_value)
    if not target.is_absolute():
        target = root / target
    target = target.resolve(strict=False)
    return target != root and target.is_relative_to(root)
