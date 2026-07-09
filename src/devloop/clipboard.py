from __future__ import annotations

import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Callable, Sequence

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[bytes]"]

_WINDOWS_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "Add-Type -AssemblyName System.Windows.Forms; "
    "$img = Get-Clipboard -Format Image; "
    "if ($null -eq $img) {{ exit 1 }}; "
    "$img.Save('{dest}', [System.Drawing.Imaging.ImageFormat]::Png); "
    "exit 0"
)


def _default_runner(command: Sequence[str]) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.run(list(command), capture_output=True, check=False)


def capture_clipboard_image(
    dest_dir: Path,
    *,
    runner: Runner | None = None,
    platform_name: str | None = None,
) -> Path | None:
    runner = runner or _default_runner
    platform_name = platform_name or sys.platform
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"clipboard-{uuid.uuid4().hex}.png"

    if platform_name.startswith("win"):
        return _capture_windows(dest, runner)
    if platform_name.startswith("darwin"):
        return _capture_macos(dest, runner)
    return _capture_linux(dest, runner)


def _capture_windows(dest: Path, runner: Runner) -> Path | None:
    script = _WINDOWS_SCRIPT.format(dest=str(dest).replace("'", "''"))
    command = ["powershell.exe", "-NoProfile", "-Command", script, str(dest)]
    try:
        result = runner(command)
    except FileNotFoundError:
        print("Clipboard capture needs powershell.exe on PATH.", file=sys.stderr)
        return None
    if result.returncode != 0 or not dest.is_file():
        return None
    return dest


def _capture_linux(dest: Path, runner: Runner) -> Path | None:
    attempts = [
        ["wl-paste", "--type", "image/png"],
        ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
    ]
    missing: list[str] = []
    for command in attempts:
        try:
            result = runner(command)
        except FileNotFoundError:
            missing.append(command[0])
            continue
        if result.returncode == 0 and result.stdout:
            dest.write_bytes(result.stdout)
            return dest
    if len(missing) == len(attempts):
        print(
            "Clipboard capture needs wl-paste (Wayland) or xclip (X11) installed.",
            file=sys.stderr,
        )
    return None


def _capture_macos(dest: Path, runner: Runner) -> Path | None:
    try:
        result = runner(["pngpaste", str(dest)])
        if result.returncode == 0 and dest.is_file():
            return dest
    except FileNotFoundError:
        pass

    try:
        result = runner(["osascript", "-e", "the clipboard as «class PNGf»"])
    except FileNotFoundError:
        print("Clipboard capture needs pngpaste (brew install pngpaste).", file=sys.stderr)
        return None
    if result.returncode != 0:
        return None
    match = re.search(rb"data PNGf([0-9A-Fa-f]+)", result.stdout)
    if not match:
        return None
    dest.write_bytes(bytes.fromhex(match.group(1).decode("ascii")))
    return dest
