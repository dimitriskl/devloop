from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_SELF_IMPROVEMENT_WIKI_PATH = "docs/devloop-self-improvement/wiki"


def resolve_self_improvement_wiki_path(base_root: Path, path_text: str) -> Path:
    path = Path(path_text).expanduser()
    resolved = path.resolve() if path.is_absolute() else (base_root / path).resolve()
    ensure_path_is_under_root(base_root=base_root, path=resolved)
    return resolved


def ensure_path_is_under_root(base_root: Path, path: Path) -> None:
    try:
        path.relative_to(base_root.resolve())
    except ValueError as exc:
        raise ValueError(f"Self-improvement wiki path must be inside the Dev Loop bundle root: {path}") from exc


def ensure_self_improvement_wiki(wiki_root: Path) -> list[Path]:
    wiki_root.mkdir(parents=True, exist_ok=True)

    created: list[Path] = []
    files = {
        wiki_root.parent / "SCHEMA.md": schema_text(),
        wiki_root / "index.md": index_text(),
        wiki_root / "lessons-learned.md": lessons_text(),
    }

    for path, text in files.items():
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        created.append(path)

    return created


def write_self_improvement_context(
    wiki_root: Path,
    *,
    state: dict[str, Any],
    state_path: Path,
    board_path: Path,
    target_repo_root: Path,
    prd_path: Path,
    issues_index: Path,
) -> Path:
    run_root = wiki_root.parent / ".compiler-runs"
    run_root.mkdir(parents=True, exist_ok=True)
    path = run_root / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-context.md"
    context = {
        "target_repo_root": str(target_repo_root),
        "prd_path": str(prd_path),
        "issues_index": str(issues_index),
        "loop_state_path": str(state_path),
        "loop_board_path": str(board_path),
        "loop_state": state,
    }
    path.write_text(
        "# Dev Loop Self-Improvement Context\n\n"
        "This generated context is sanitized runner state for the self-improvement compiler. "
        "It intentionally omits raw stdout, stderr, credentials, and large logs.\n\n"
        "```json\n"
        f"{json.dumps(context, indent=2, ensure_ascii=False)}\n"
        "```\n",
        encoding="utf-8",
    )
    return path


def schema_text() -> str:
    return """# Dev Loop Self-Improvement Schema

The Dev Loop self-improvement wiki stores durable lessons that improve future
Dev Loop runs. It belongs to the Dev Loop bundle, not to any one target
repository.

## Lesson Entry Format

```md
## Short Lesson Title

- Applies to: runner area, workflow, target repo family, or technology
- Lesson: one reusable rule or pattern
- Evidence: loop state, gate feedback, issue file, or command output reference
- Action: what the next agent should do differently
- Last seen: YYYY-MM-DD
```

## Promotion Rules

- Keep only lessons likely to matter in future runs.
- Prefer user instructions, implementation lessons, bug causes and fixes,
  repeated failures, blocked causes, reviewer/QA feedback, and successful
  patterns that avoided rework.
- Store target-repo facts only when they teach Dev Loop how to work better in
  future runs. Do not use this wiki as a business-domain notebook.
- Do not store raw logs, secrets, credentials, tokens, personal data, or large
  code blocks.
- Update an existing lesson instead of creating a duplicate when the meaning is
  the same.
"""


def index_text() -> str:
    return """# Dev Loop Self-Improvement Wiki

This wiki is compiled from Dev Loop runs and belongs to the Dev Loop runner. It
stores general user instructions, implementation lessons, bugs and fixes, and
workflow improvements that should benefit future runs across target projects.

## Pages

- [Lessons Learned](lessons-learned.md)

## Maintenance

Dev Loop updates this wiki at the end of real runs unless
`--no-self-improvement-wiki` is used. Review changes like any other runner
documentation.
"""


def lessons_text() -> str:
    return """# Dev Loop Self-Improvement Lessons

Durable, evidence-backed lessons that improve future Dev Loop runs.

## Entries
"""
