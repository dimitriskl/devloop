from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from .gitrefs import sanitize_branch_name
from .lineeditor import LineEditor
from .subprocess_utils import run_captured_text

_PROMPT_EDITOR: LineEditor | None = None


@dataclass(frozen=True)
class WorktreeSelection:
    repo_root: Path
    created: bool


@dataclass(frozen=True)
class ExistingWorktree:
    repo_root: Path
    branch: str


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
        answer = read_prompt("Create a dedicated implementation worktree? [Y/n] ").strip().lower()
        create_worktree = not answer or answer in {"y", "yes"}

    if not create_worktree:
        return WorktreeSelection(repo_root=source_repo, created=False)

    if worktree_path is None:
        if not interactive:
            raise ValueError("--worktree-path is required with --create-worktree in non-interactive mode.")
        worktree_path = ask_worktree_location("Implementation worktree")

    if not branch_name:
        if not interactive:
            raise ValueError("--branch-name is required with --create-worktree in non-interactive mode.")
        branch_name = ask_branch_name("Implementation branch name")

    if not branch_name:
        raise ValueError("Branch name cannot be empty.")

    sanitized_branch_name = sanitize_branch_name(branch_name)
    if sanitized_branch_name != branch_name:
        print(f"Using branch name: {sanitized_branch_name}")
    branch_name = sanitized_branch_name

    worktree_path = worktree_path.resolve()
    existing_worktree = resolve_existing_worktree(source_repo, worktree_path, branch_name)
    if existing_worktree is not None:
        return WorktreeSelection(repo_root=existing_worktree, created=False)

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


def resolve_existing_worktree(source_repo: Path, worktree_path: Path, branch_name: str) -> Path | None:
    existing_worktree = find_existing_worktree(source_repo, worktree_path)
    if existing_worktree is None:
        return None

    if existing_worktree.branch and not branch_matches(existing_worktree.branch, branch_name):
        print(
            "Using existing worktree: "
            f"{existing_worktree.repo_root} "
            f"(branch {display_branch(existing_worktree.branch)}; requested {branch_name})"
        )
        return existing_worktree.repo_root

    print(f"Using existing worktree: {existing_worktree.repo_root}")
    return existing_worktree.repo_root


def find_existing_worktree(source_repo: Path, worktree_path: Path) -> ExistingWorktree | None:
    worktree_path = worktree_path.resolve()
    registered_worktree = find_registered_worktree(source_repo, worktree_path)
    if registered_worktree is not None:
        return ExistingWorktree(
            repo_root=worktree_path,
            branch=registered_worktree.get("branch", ""),
        )

    if not worktree_path.exists():
        return None

    if not worktree_path.is_dir():
        raise RuntimeError(f"Requested worktree path already exists but is not a directory: {worktree_path}")

    if is_empty_directory(worktree_path):
        return None

    checkout = find_git_checkout(worktree_path)
    if checkout is None:
        raise RuntimeError(
            "Requested worktree path already exists but is not an empty folder or Git checkout: "
            f"{worktree_path}"
        )

    if checkout.repo_root != worktree_path:
        raise RuntimeError(
            "Requested worktree path is inside another Git checkout. "
            f"Use the checkout root instead: {checkout.repo_root}"
        )

    return checkout


def find_git_checkout(path: Path) -> ExistingWorktree | None:
    root_result = run_captured_text(["git", "rev-parse", "--show-toplevel"], cwd=path)
    if root_result.returncode != 0:
        return None

    repo_root = Path(root_result.stdout.strip()).resolve()
    branch_result = run_captured_text(["git", "branch", "--show-current"], cwd=repo_root)
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else ""
    return ExistingWorktree(repo_root=repo_root, branch=branch)


def is_empty_directory(path: Path) -> bool:
    return not any(path.iterdir())


def build_worktree_add_command(source_repo: Path, worktree_path: Path, branch_name: str) -> list[str]:
    if branch_exists(source_repo, branch_name):
        return ["git", "worktree", "add", str(worktree_path), branch_name]
    return ["git", "worktree", "add", "-b", branch_name, str(worktree_path)]


def read_prompt(prompt: str) -> str:
    global _PROMPT_EDITOR
    if _PROMPT_EDITOR is None:
        _PROMPT_EDITOR = LineEditor(on_paste_image=lambda: None, fallback_hint=None)
    return _PROMPT_EDITOR.read_line(prompt)


def ask_required(prompt: str, *, default: str | None = None) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        value = read_prompt(f"{prompt}{suffix}: ").strip()
        if value:
            return value
        if default:
            return default
        print("Value is required.", file=sys.stderr)


def ask_branch_name(prompt: str, *, default: str | None = None) -> str:
    raw = ask_required(prompt, default=default)
    branch_name = sanitize_branch_name(raw)
    if branch_name != raw:
        print(f"Using branch name: {branch_name}")
    return branch_name


def ask_path(prompt: str, *, default: Path | None = None) -> Path:
    value = ask_required(prompt, default=str(default) if default else None)
    return Path(value).expanduser().resolve()


def ask_worktree_location(prompt: str, *, default: Path | None = None) -> Path:
    default_parent = default.parent if default is not None else None
    default_name = default.name if default is not None else None
    while True:
        parent = ask_path(f"{prompt} parent path", default=default_parent)
        name = ask_required(f"{prompt} folder name", default=default_name)
        name_path = Path(name)
        if name_path.is_absolute() or len(name_path.parts) != 1:
            print("Enter only the worktree folder name, not a full path.", file=sys.stderr)
            continue
        return (parent / name).expanduser().resolve()


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
