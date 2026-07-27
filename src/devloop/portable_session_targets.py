from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .worktree import find_git_checkout, resolve_worktree


@dataclass(frozen=True)
class SavedWorktreeTarget:
    checkout: Path


@dataclass(frozen=True)
class ExistingCheckoutTarget:
    checkout: Path


@dataclass(frozen=True)
class NewWorktreeTarget:
    repository: Path
    checkout: Path
    branch: str


PortableSessionTargetRequest = (
    SavedWorktreeTarget | ExistingCheckoutTarget | NewWorktreeTarget
)


@dataclass(frozen=True)
class PortableSessionTarget:
    checkout: Path
    created: bool
    notices: tuple[str, ...] = ()


class PortableSessionTargetController(Protocol):
    def resolve(
        self,
        request: PortableSessionTargetRequest,
    ) -> PortableSessionTarget: ...


class PortableSessionTargetResolver:
    """Resolve every new-session choice to one exact Git checkout root."""

    def resolve(self, request: PortableSessionTargetRequest) -> PortableSessionTarget:
        if isinstance(request, NewWorktreeTarget):
            repository = self._canonical_checkout(request.repository)
            notices: list[str] = []
            selection = resolve_worktree(
                source_repo=repository,
                create_worktree=True,
                no_worktree=False,
                worktree_path=request.checkout,
                branch_name=request.branch,
                interactive=False,
                dry_run=False,
                notice=notices.append,
            )
            return PortableSessionTarget(
                checkout=self._canonical_checkout(selection.repo_root),
                created=selection.created,
                notices=tuple(notices),
            )
        return PortableSessionTarget(
            checkout=self._canonical_checkout(request.checkout),
            created=False,
        )

    @staticmethod
    def _canonical_checkout(path: Path) -> Path:
        selected = path.expanduser().resolve()
        if not selected.is_dir():
            raise ValueError(f"Portable session checkout does not exist: {selected}")
        checkout = find_git_checkout(selected)
        if checkout is None:
            raise ValueError(
                f"Portable session target is not a Git checkout: {selected}"
            )
        return checkout.repo_root.resolve()
