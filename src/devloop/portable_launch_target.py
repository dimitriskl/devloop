from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .issue_pack import find_repo_root
from .subprocess_utils import run_captured_text

def planning_launch_checkout(
    *,
    current_checkout: Path,
    repo_argument: str | None,
    prd_argument: str | None,
) -> Path:
    """Resolve the checkout that a planning entry point must lease first."""
    repo_checkout = _argument_checkout(
        repo_argument,
        current_checkout=current_checkout,
        directory_argument=True,
    )
    prd_checkout = _argument_checkout(
        prd_argument,
        current_checkout=current_checkout,
        directory_argument=False,
    )
    if repo_checkout is not None and prd_checkout is not None and repo_checkout != prd_checkout:
        raise ValueError(
            "--repo and --prd resolve to different Git checkouts: "
            f"{repo_checkout} and {prd_checkout}"
        )
    return prd_checkout or repo_checkout or current_checkout.resolve()

if TYPE_CHECKING:
    from .portable_sessions import PortableSessionLaunch


@dataclass(frozen=True)
class PortableLaunchTarget:
    checkout: Path
    argument_base: Path
    creates_checkout: bool


def validated_portable_launch_target(
    launch: PortableSessionLaunch,
) -> PortableLaunchTarget:
    """Validate a launch's leased checkout and its trusted argv resolution base."""
    checkout = launch.checkout.resolve()
    argument_base = _validated_argument_base(launch.argument_base, checkout=checkout)
    if launch.operation.value == "DELIVERY":
        prd_argument = _option_value(launch.arguments, "--prd")
        if prd_argument is None:
            if not checkout.is_dir():
                raise ValueError("Portable delivery launch requires --prd.")
            return PortableLaunchTarget(
                checkout=checkout,
                argument_base=argument_base,
                creates_checkout=False,
            )
        expected = delivery_launch_checkout(
            current_checkout=argument_base,
            prd_argument=prd_argument,
            issues_argument=_option_value(launch.arguments, "--issues"),
            create_worktree="--create-worktree" in launch.arguments,
            worktree_path_argument=_option_value(
                launch.arguments,
                "--worktree-path",
            ),
            dry_run="--dry-run" in launch.arguments,
        )
    else:
        expected = planning_launch_checkout(
            current_checkout=argument_base,
            repo_argument=_option_value(launch.arguments, "--repo"),
            prd_argument=_option_value(launch.arguments, "--prd"),
        )
    if not checkout.is_dir() and expected.resolve() != checkout:
        raise ValueError(
            "Portable session checkout does not match its launch arguments: "
            f"{checkout} != {expected.resolve()}"
        )
    creates_checkout = (
        launch.operation.value == "DELIVERY"
        and "--create-worktree" in launch.arguments
        and "--dry-run" not in launch.arguments
        and _option_value(launch.arguments, "--worktree-path") is not None
        and not checkout.exists()
    )
    if not checkout.is_dir() and not creates_checkout:
        raise ValueError(f"Portable session checkout does not exist: {checkout}")
    return PortableLaunchTarget(
        checkout=checkout,
        argument_base=argument_base,
        creates_checkout=creates_checkout,
    )


def launch_creates_checkout(
    launch: PortableSessionLaunch,
    checkout: Path,
) -> bool:
    """Return whether validated delivery argv will create exactly checkout."""
    try:
        target = validated_portable_launch_target(launch)
    except (OSError, RuntimeError, ValueError):
        return False
    return target.creates_checkout and target.checkout == checkout.resolve()


def delivery_launch_checkout(
    *,
    current_checkout: Path,
    prd_argument: str,
    issues_argument: str | None,
    create_worktree: bool,
    worktree_path_argument: str | None,
    dry_run: bool,
) -> Path:
    """Resolve the checkout that a delivery entry point must lease first."""
    source_checkout = _argument_checkout(
        issues_argument or prd_argument,
        current_checkout=current_checkout,
        directory_argument=False,
    )
    prd_checkout = _argument_checkout(
        prd_argument,
        current_checkout=current_checkout,
        directory_argument=False,
    )
    if source_checkout is not None and prd_checkout is not None and source_checkout != prd_checkout:
        raise ValueError(
            "--prd and --issues resolve to different Git checkouts: "
            f"{prd_checkout} and {source_checkout}"
        )
    if create_worktree:
        requested_worktree = _argument_path(
            worktree_path_argument,
            current_checkout=current_checkout,
            directory_argument=True,
        )
        if requested_worktree is not None and source_checkout is not None:
            _validate_requested_worktree(source_checkout, requested_worktree)
        worktree_checkout = _argument_checkout(
            worktree_path_argument,
            current_checkout=current_checkout,
            directory_argument=True,
        )
        if worktree_checkout is not None:
            return worktree_checkout
        if requested_worktree is not None and not dry_run:
            return requested_worktree
    return source_checkout or prd_checkout or current_checkout.resolve()


def _argument_checkout(
    argument: str | None,
    *,
    current_checkout: Path,
    directory_argument: bool,
) -> Path | None:
    if argument is None or not argument.strip():
        return None
    path = _resolve_from(argument, current_checkout)
    if not path.exists():
        return None
    start = path if path.is_dir() else path.parent
    if directory_argument and not path.is_dir():
        return None
    try:
        return find_repo_root(start)
    except RuntimeError:
        return None


def _argument_path(
    argument: str | None,
    *,
    current_checkout: Path,
    directory_argument: bool,
) -> Path | None:
    if argument is None or not argument.strip():
        return None
    path = _resolve_from(argument, current_checkout)
    if path.exists() and (not directory_argument or not path.is_dir()):
        return None
    return path


def _resolve_from(value: str, current_checkout: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = current_checkout / path
    return path.resolve()


def _validated_argument_base(
    argument_base: Path | None,
    *,
    checkout: Path,
) -> Path:
    candidate = checkout if argument_base is None else argument_base
    resolved = candidate.expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(
            "Portable session argument base does not exist or is not a directory: "
            f"{resolved}"
        )
    return resolved


def _option_value(arguments: Sequence[str], option: str) -> str | None:
    for index, argument in enumerate(arguments):
        if argument == option:
            if index + 1 >= len(arguments):
                raise ValueError(f"Portable launch option {option} has no value.")
            return arguments[index + 1]
        prefix = option + "="
        if argument.startswith(prefix):
            value = argument[len(prefix) :]
            if not value:
                raise ValueError(f"Portable launch option {option} has no value.")
            return value
    return None


def _validate_requested_worktree(source_checkout: Path, requested: Path) -> None:
    if requested == source_checkout or requested.is_relative_to(source_checkout):
        raise ValueError(f"--worktree-path must be outside the source checkout: {requested}")
    if not requested.exists() or not any(requested.iterdir()):
        return
    requested_checkout = _argument_checkout(
        str(requested),
        current_checkout=source_checkout,
        directory_argument=True,
    )
    if requested_checkout != requested:
        raise ValueError(
            f"--worktree-path exists but is not a Git worktree root or empty directory: {requested}"
        )
    if _git_common_directory(source_checkout) != _git_common_directory(requested):
        raise ValueError(f"--worktree-path belongs to a different Git repository: {requested}")


def _git_common_directory(checkout: Path) -> Path:
    result = run_captured_text(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=checkout,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ValueError(f"Git worktree is not usable: {checkout}")
    common_directory = Path(result.stdout.strip())
    if not common_directory.is_absolute():
        common_directory = checkout / common_directory
    return common_directory.resolve()
