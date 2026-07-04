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

    worktree_path = worktree_path.resolve()
    existing_worktree = find_registered_worktree(source_repo, worktree_path)
    if existing_worktree is not None:
        existing_branch = existing_worktree.get("branch", "")
        if not branch_matches(existing_branch, branch_name):
            raise RuntimeError(
                "Requested worktree path is already registered on branch "
                f"'{display_branch(existing_branch)}', not '{branch_name}'."
            )
        return WorktreeSelection(repo_root=worktree_path, created=False)

    command = build_worktree_add_command(source_repo, worktree_path, branch_name)

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


def build_worktree_add_command(source_repo: Path, worktree_path: Path, branch_name: str) -> list[str]:
    if branch_exists(source_repo, branch_name):
        return ["git", "worktree", "add", str(worktree_path), branch_name]
    return ["git", "worktree", "add", "-b", branch_name, str(worktree_path)]


def branch_exists(source_repo: Path, branch_name: str) -> bool:
    result = run_captured_text(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        cwd=source_repo,
    )
    return result.returncode == 0


def find_registered_worktree(source_repo: Path, worktree_path: Path) -> dict[str, str] | None:
    result = run_captured_text(["git", "worktree", "list", "--porcelain"], cwd=source_repo)
    if result.returncode != 0:
        raise RuntimeError(f"git worktree list failed: {result.stderr.strip()}")

    requested = worktree_path.resolve()
    for item in parse_worktree_list(result.stdout):
        item_path = Path(item.get("path", "")).resolve()
        if item_path == requested:
            return item

    return None


def parse_worktree_list(output: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    current: dict[str, str] | None = None

    for line in output.splitlines():
        if not line.strip():
            if current is not None:
                items.append(current)
                current = None
            continue

        if line.startswith("worktree "):
            if current is not None:
                items.append(current)
            current = {"path": line.removeprefix("worktree ")}
        elif line.startswith("branch ") and current is not None:
            current["branch"] = line.removeprefix("branch ")

    if current is not None:
        items.append(current)

    return items


def branch_matches(existing_branch: str, requested_branch: str) -> bool:
    return existing_branch == requested_branch or existing_branch == f"refs/heads/{requested_branch}"


def display_branch(branch: str) -> str:
    return branch.removeprefix("refs/heads/") or "detached"
