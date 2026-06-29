from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .subprocess_utils import run_captured_text


@dataclass(frozen=True)
class Issue:
    number: str
    title: str
    path: Path
    completed: bool

    @staticmethod
    def is_completed_file(path: Path) -> bool:
        if not path.is_file():
            return False
        text = path.read_text(encoding="utf-8")
        return is_completed_text(text)


LINK_PATTERN = re.compile(r"\[(?P<title>[^\]]+)\]\((?P<href>[^)]+\.md)\)")
NUMBER_PATTERN = re.compile(r"(?P<number>\d{1,5})")


def find_repo_root(start: Path) -> Path:
    result = run_captured_text(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not find git repository root from {start}: {result.stderr.strip()}")
    return Path(result.stdout.strip()).resolve()


def parse_issue_index(index_path: Path) -> list[Issue]:
    text = index_path.read_text(encoding="utf-8")
    issues: list[Issue] = []
    issue_root = index_path.parent.resolve()

    for match in LINK_PATTERN.finditer(text):
        href = match.group("href")
        title = match.group("title").strip()
        issue_path = (issue_root / href).resolve()

        try:
            issue_path.relative_to(issue_root)
        except ValueError:
            continue

        if not issue_path.is_file():
            continue

        number = issue_number(issue_path, title)
        issues.append(
            Issue(
                number=number,
                title=title,
                path=issue_path,
                completed=Issue.is_completed_file(issue_path),
            )
        )

    return issues


def issue_number(path: Path, title: str) -> str:
    filename_match = NUMBER_PATTERN.search(path.stem)
    if filename_match:
        return filename_match.group("number").zfill(4)

    title_match = NUMBER_PATTERN.search(title)
    if title_match:
        return title_match.group("number").zfill(4)

    return path.stem


def select_issues(issues: list[Issue], run_all: bool, start_issue: str | None) -> list[Issue]:
    pending = [issue for issue in issues if not issue.completed]

    if start_issue:
        start_key = start_issue.strip().lower()
        candidates = [
            issue
            for issue in issues
            if issue.number.lower().lstrip("0") == start_key.lstrip("0")
            or issue.path.name.lower().startswith(start_key)
            or issue.path.stem.lower().startswith(start_key)
        ]
        if not candidates:
            raise ValueError(f"No issue matches --start-issue {start_issue}")

        start = candidates[0]
        start_index = issues.index(start)
        selected = [issue for issue in issues[start_index:] if not issue.completed]
        return selected if run_all else selected[:1]

    if run_all:
        return pending

    return pending[:1]


def is_completed_text(text: str) -> bool:
    if re.search(r"(?im)^Completed:\s*\[[xX]\]", text):
        return True

    notes_match = re.search(r"(?ims)^## Implementation Notes\b(?P<body>.*?)(?=^## |\Z)", text)
    if not notes_match:
        return False

    for line in notes_match.group("body").splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith("completed:"):
            continue
        value = stripped.split(":", 1)[1].strip()
        if value and not re.fullmatch(r"\[\s*\]", value):
            return True

    return False
