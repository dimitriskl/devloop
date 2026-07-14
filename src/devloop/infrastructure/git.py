from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path
from typing import Protocol

from devloop.domain.development import ChangedFile, ChangeKind, WorkspaceBaselineEntry


class GitOperationError(RuntimeError):
    pass


MAX_REVIEW_FILE_BYTES = 1 * 1024 * 1024
MAX_RELEVANT_DIFF_CHARS = 200_000


@dataclass(frozen=True)
class GitResult:
    output: str


def run_git(
    repository: Path,
    arguments: tuple[str, ...],
    *,
    timeout: float = 30.0,
    fail_on_stderr: bool = False,
) -> GitResult:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise GitOperationError("Git could not execute the requested local operation.") from error
    if completed.returncode != 0:
        raise GitOperationError(
            f"Git local operation failed: {' '.join(arguments[:2]) or 'command'}."
        )
    if fail_on_stderr and completed.stderr.strip():
        raise GitOperationError("Git returned a warning that makes repository state incomplete.")
    return GitResult(completed.stdout.rstrip("\r\n"))


def repository_root(repository: Path) -> Path:
    return Path(run_git(repository, ("rev-parse", "--show-toplevel")).output).resolve()


def head_commit(repository: Path) -> str:
    value = run_git(repository, ("rev-parse", "HEAD")).output
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value.lower()):
        raise GitOperationError("Git returned an invalid HEAD commit.")
    return value


def current_branch(repository: Path) -> str | None:
    value = run_git(repository, ("branch", "--show-current")).output
    return value or None


def validate_branch_name(repository: Path, branch: str) -> None:
    run_git(repository, ("check-ref-format", "--branch", branch))


def create_worktree(repository: Path, path: Path, branch: str, base_commit: str) -> None:
    validate_branch_name(repository, branch)
    if path.exists():
        raise GitOperationError(f"Proposed worktree path already exists: {path}.")
    path.parent.mkdir(parents=True, exist_ok=True)
    run_git(
        repository,
        ("worktree", "add", "-b", branch, str(path), base_commit),
        timeout=120.0,
    )
    if repository_root(path) != path.resolve():
        raise GitOperationError("Git created the worktree at an unexpected repository root.")


@dataclass(frozen=True)
class WorktreeChanges:
    base_state: str
    result_state: str
    diff_hash: str
    changed_files: tuple[ChangedFile, ...]
    repository_state_hash: str


class _Digest(Protocol):
    def update(self, value: bytes) -> None: ...


def capture_workspace_baseline(workspace: Path) -> tuple[WorkspaceBaselineEntry, ...]:
    status = run_git(
        workspace,
        ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
        fail_on_stderr=True,
    ).output
    entries: list[WorkspaceBaselineEntry] = []
    for changed in parse_porcelain(status):
        path = (workspace / changed.path).resolve()
        content_hash = (
            None if changed.kind is ChangeKind.DELETED else _content_hash(workspace, path)
        )
        entries.append(WorkspaceBaselineEntry(changed.path, changed.kind, content_hash))
    return tuple(entries)


def capture_repository_state_hash(workspace: Path) -> str:
    if repository_root(workspace) != workspace.resolve():
        raise GitOperationError("Development workspace is not its own Git worktree root.")
    status = run_git(
        workspace,
        ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
        fail_on_stderr=True,
    ).output
    return _repository_state_hash(workspace, status)


def capture_worktree_changes(
    workspace: Path,
    base_commit: str,
    baseline: tuple[WorkspaceBaselineEntry, ...] = (),
) -> WorktreeChanges:
    if repository_root(workspace) != workspace.resolve():
        raise GitOperationError("Development workspace is not its own Git worktree root.")
    status = run_git(
        workspace,
        ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
        fail_on_stderr=True,
    ).output
    repository_state_hash = _repository_state_hash(workspace, status)
    changed_files = parse_porcelain(status)
    digest = hashlib.sha256()
    baseline_by_path = {item.path: item for item in baseline}
    current_by_path = {item.path: item for item in changed_files}
    development_changes: list[ChangedFile] = []
    for changed in changed_files:
        path = (workspace / changed.path).resolve()
        content_hash = (
            None if changed.kind is ChangeKind.DELETED else _content_hash(workspace, path)
        )
        previous = baseline_by_path.get(changed.path)
        if (
            previous is not None
            and previous.kind is changed.kind
            and previous.content_hash == content_hash
        ):
            continue
        development_changes.append(changed)
        _update_change_digest(digest, changed, content_hash)
    for previous in baseline:
        if previous.path in current_by_path:
            continue
        path = (workspace / previous.path).resolve()
        if path.exists():
            changed = ChangedFile(previous.path, ChangeKind.MODIFIED)
            content_hash = _content_hash(workspace, path)
        else:
            changed = ChangedFile(previous.path, ChangeKind.DELETED)
            content_hash = None
        if content_hash == previous.content_hash:
            continue
        development_changes.append(changed)
        _update_change_digest(digest, changed, content_hash)
    diff_hash = digest.hexdigest()
    result_state = hashlib.sha256(f"{base_commit}:{diff_hash}".encode("ascii")).hexdigest()
    return WorktreeChanges(
        base_commit,
        result_state,
        diff_hash,
        tuple(development_changes),
        repository_state_hash,
    )


def _repository_state_hash(workspace: Path, status: str) -> str:
    current_head = head_commit(workspace)
    branch = current_branch(workspace) or ""
    index_state = run_git(
        workspace,
        ("ls-files", "--stage", "-z"),
        fail_on_stderr=True,
    ).output
    digest = hashlib.sha256()
    digest.update(f"{current_head}\0{branch}\0{status}\0{index_state}\0".encode())
    for changed in parse_porcelain(status):
        path = (workspace / changed.path).resolve()
        content_hash = (
            None if changed.kind is ChangeKind.DELETED else _content_hash(workspace, path)
        )
        _update_change_digest(digest, changed, content_hash)
    return digest.hexdigest()


def render_relevant_diff(
    workspace: Path,
    base_commit: str,
    changed_files: tuple[ChangedFile, ...],
) -> str:
    root = workspace.resolve()
    rendered: list[str] = []
    for changed in changed_files:
        relative = Path(changed.path)
        current_path = (root / relative).resolve()
        if current_path != root and not current_path.is_relative_to(root):
            raise GitOperationError("Implementation Result contains an unsafe file path.")
        before = _git_file_at_commit(root, base_commit, changed.path)
        after = "" if changed.kind is ChangeKind.DELETED else _review_text(current_path)
        rendered.extend(
            unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{changed.path}",
                tofile=f"b/{changed.path}",
                n=3,
            )
        )
        if sum(len(item) for item in rendered) > MAX_RELEVANT_DIFF_CHARS:
            raise GitOperationError("Relevant implementation diff exceeds the review limit.")
    return "".join(rendered)


def _git_file_at_commit(workspace: Path, commit: str, relative_path: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(workspace), "show", f"{commit}:{relative_path}"],
            capture_output=True,
            check=False,
            timeout=30.0,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise GitOperationError("Git could not read the base implementation file.") from error
    if completed.returncode != 0:
        return ""
    if len(completed.stdout) > MAX_REVIEW_FILE_BYTES:
        raise GitOperationError("A base implementation file exceeds the review limit.")
    return completed.stdout.decode("utf-8", errors="replace")


def _review_text(path: Path) -> str:
    try:
        if not path.is_file() or path.stat().st_size > MAX_REVIEW_FILE_BYTES:
            raise GitOperationError("An implementation file is unavailable for review.")
        return path.read_bytes().decode("utf-8", errors="replace")
    except OSError as error:
        raise GitOperationError("An implementation file cannot be read for review.") from error


def _content_hash(workspace: Path, path: Path) -> str:
    try:
        if not path.is_relative_to(workspace.resolve()) or not path.is_file():
            raise GitOperationError("Development output is unsafe or unavailable.")
        if path.stat().st_size > 20 * 1024 * 1024:
            raise GitOperationError("A development file exceeds the hash limit.")
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise GitOperationError(
            f"Development output cannot be read for hashing: {path.name}."
        ) from error


def _update_change_digest(
    digest: _Digest,
    changed: ChangedFile,
    content_hash: str | None,
) -> None:
    digest.update(changed.path.encode("utf-8"))
    digest.update(changed.kind.value.encode("ascii"))
    if content_hash is not None:
        digest.update(content_hash.encode("ascii"))


def parse_porcelain(value: str) -> tuple[ChangedFile, ...]:
    if not value:
        return ()
    entries = value.split("\0")
    changes: list[ChangedFile] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        if len(entry) < 4 or entry[2] != " ":
            raise GitOperationError("Git status returned an invalid porcelain record.")
        status = entry[:2]
        path = entry[3:]
        if "R" in status or "C" in status:
            if index >= len(entries) or not entries[index]:
                raise GitOperationError("Git status returned an incomplete rename record.")
            # Porcelain v1 with -z reports the destination in the status record and
            # the source in the following NUL-delimited field. The result contract
            # identifies the path that exists after development.
            index += 1
            kind = ChangeKind.RENAMED
        elif status == "??":
            kind = ChangeKind.UNTRACKED
        elif "D" in status:
            kind = ChangeKind.DELETED
        elif "A" in status:
            kind = ChangeKind.ADDED
        else:
            kind = ChangeKind.MODIFIED
        changes.append(ChangedFile(path.replace("\\", "/"), kind))
    return tuple(changes)
