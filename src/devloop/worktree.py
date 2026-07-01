from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .subprocess_utils import run_captured_text


@dataclass(frozen=True)
class WorktreeSelection:
    repo_root: Path
    created: bool


def resolve_worktree(
    source_repo: Path,
    create_worktree: bool,
    no_worktree: bool,
    worktree_path: Path | None,
    branch_name: str | None,
    interactive: bool,
    dry_run: bool,
) -> WorktreeSelection:
    if create_worktree and no_worktree:
        raise ValueError("Use only one of --create-worktree or --no-worktree.")

    if no_worktree:
        return WorktreeSelection(repo_root=source_repo, created=False)

    if not create_worktree and not interactive:
        return WorktreeSelection(repo_root=source_repo, created=False)

    if not create_worktree and interactive:
        answer = input("Create a dedicated implementation worktree? [Y/n] ").strip().lower()
        create_worktree = not answer or answer in {"y", "yes"}

    if not create_worktree:
        return WorktreeSelection(repo_root=source_repo, created=False)

    if worktree_path is None:
        if not interactive:
            raise ValueError("--worktree-path is required with --create-worktree in non-interactive mode.")
        worktree_path = Path(input("Implementation worktree path: ").strip()).expanduser().resolve()

    if not branch_name:
        if not interactive:
            raise ValueError("--branch-name is required with --create-worktree in non-interactive mode.")
        branch_name = input("Implementation branch name: ").strip()

    if not branch_name:
        raise ValueError("Branch name cannot be empty.")

    command = ["git", "worktree", "add", "-b", branch_name, str(worktree_path)]

    if dry_run:
        print(f"[dry-run] Would run in {source_repo}: {' '.join(command)}")
        return WorktreeSelection(repo_root=worktree_path, created=False)

    result = run_captured_text(
        command,
        cwd=source_repo,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {result.stderr.strip()}")

    return WorktreeSelection(repo_root=worktree_path.resolve(), created=True)
