from __future__ import annotations

import sys
import re
from pathlib import Path
from typing import Sequence

from .version import VERSION

VERSION_TOKEN = "{VERSION}"
VERSION_PATTERN = re.compile(r"v\d+\.\d+\.\d+")

DEFAULT_LOGO_TEMPLATE = """+------------------------------------------------------------+
|  ____  _______     __  _     ___   ___  ____              |
| |  _ \\| ____\\ \\   / / | |   / _ \\ / _ \\|  _ \\             |
| | | | |  _|  \\ \\ / /  | |  | | | | | | | |_) |            |
| | |_| | |___  \\ V /   | |__| |_| | |_| |  __/             |
| |____/|_____|  \\_/    |_____\\___/ \\___/|_|     v0.1.0 |
|                                                            |
|    [ ANALYSIS ] => [ BUILD ] => [ REVIEW ] => [ QA ]      |
+------------------------------------------------------------+
"""


def render_logo(bundle_root: Path | None = None, *, version: str = VERSION) -> str:
    template = load_logo_template(bundle_root)
    return align_logo(apply_logo_version(template, version))


def apply_logo_version(template: str, version: str) -> str:
    if VERSION_TOKEN in template:
        return template.replace(VERSION_TOKEN, version)
    replacement = f"v{version}"
    rendered, count = VERSION_PATTERN.subn(replacement, template, count=1)
    return rendered if count else template


def load_logo_template(bundle_root: Path | None) -> str:
    if bundle_root is None:
        bundle_root = Path(__file__).resolve().parents[2]
    path = bundle_root / "docs" / "devloop-logo.txt"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return DEFAULT_LOGO_TEMPLATE


def align_logo(text: str) -> str:
    lines = text.splitlines()
    if not lines:
        return ""

    width = max(len(line) for line in lines)
    aligned = [_align_logo_line(line, width) for line in lines]
    return "\n".join(aligned) + "\n"


def _align_logo_line(line: str, width: int) -> str:
    if len(line) == width:
        return line
    if line.startswith("|") and line.endswith("|"):
        return f"{line[:-1]}{' ' * (width - len(line))}|"
    if line.startswith("+") and line.endswith("+") and set(line[1:-1]) <= {"-"}:
        return f"+{'-' * (width - 2)}+"
    return line.ljust(width)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    bundle_root = Path(args[0]).resolve() if args else None
    print(render_logo(bundle_root), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
