from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .portable_session_catalog import (
    PortableAdoptionRequest,
    PortableAdoptionSession,
    PortableResumeCandidateSource,
    PortableSessionCatalog,
)
from .subprocess_utils import run_captured_text
from .worktree import find_git_checkout, git_common_directory, parse_worktree_list

V021_SOURCE_VERSION = "0.2.1"
_TARGET_REPO_KEY = "target_repo"
_TARGET_REPO_CONFIRMED_KEY = "target_repo_confirmed"


class PortableAdoptionStatus(str, Enum):
    ADOPTED = "adopted"
    ALREADY_ADOPTED = "already adopted"
    UNAVAILABLE = "unavailable"
    IGNORED = "ignored"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class PortableAdoptionEntry:
    status: PortableAdoptionStatus
    checkout: Path | None
    detail: str
    session_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PortableAdoptionReport:
    entries: tuple[PortableAdoptionEntry, ...]

    def render(self) -> str:
        return "\n".join(
            f"{entry.status.value.title()}: "
            f"{entry.checkout if entry.checkout is not None else entry.detail}"
            f"{f' - {entry.detail}' if entry.checkout is not None else ''}"
            for entry in self.entries
        )


ResumeDiscoverer = Callable[[Path], Iterable[PortableResumeCandidateSource]]


def adopt_v021_configuration(
    catalog: PortableSessionCatalog,
    configuration_path: Path,
    *,
    discoverer: ResumeDiscoverer | None = None,
) -> PortableAdoptionReport:
    """Read a v0.2.1 planner configuration and adopt its confirmed target."""
    canonical_configuration = configuration_path.resolve()
    try:
        configuration_bytes = canonical_configuration.read_bytes()
        value = json.loads(configuration_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return _single_report(
            PortableAdoptionStatus.UNSUPPORTED,
            None,
            f"Planner configuration could not be read: {error}",
        )
    if not isinstance(value, dict):
        return _single_report(
            PortableAdoptionStatus.UNSUPPORTED,
            None,
            "Planner configuration is not a JSON object.",
        )
    if value.get(_TARGET_REPO_CONFIRMED_KEY) is not True:
        return _single_report(
            PortableAdoptionStatus.IGNORED,
            None,
            "Planner configuration has no confirmed target checkout.",
        )
    raw_target = value.get(_TARGET_REPO_KEY)
    if not isinstance(raw_target, str) or not raw_target.strip():
        return _single_report(
            PortableAdoptionStatus.UNSUPPORTED,
            None,
            "Confirmed target checkout is not a non-empty path.",
        )
    target = Path(raw_target).expanduser().resolve()
    return _adopt_checkout(
        catalog,
        target,
        configuration_path=canonical_configuration,
        configuration_sha256=hashlib.sha256(configuration_bytes).hexdigest(),
        include_related_worktrees=True,
        register_primary_without_workflow=True,
        discoverer=discoverer or _default_discoverer,
    )


def adopt_existing_checkout(
    catalog: PortableSessionCatalog,
    checkout: Path,
    *,
    discoverer: ResumeDiscoverer | None = None,
) -> PortableAdoptionReport:
    """Explicitly adopt one existing checkout selected by the user."""
    canonical_checkout = checkout.expanduser().resolve()
    identity = canonical_checkout.as_uri().encode("utf-8")
    return _adopt_checkout(
        catalog,
        canonical_checkout,
        configuration_path=canonical_checkout,
        configuration_sha256=hashlib.sha256(identity).hexdigest(),
        include_related_worktrees=False,
        register_primary_without_workflow=True,
        discoverer=discoverer or _default_discoverer,
    )


def _adopt_checkout(
    catalog: PortableSessionCatalog,
    target: Path,
    *,
    configuration_path: Path,
    configuration_sha256: str,
    include_related_worktrees: bool,
    register_primary_without_workflow: bool,
    discoverer: ResumeDiscoverer,
) -> PortableAdoptionReport:
    if not target.exists():
        if not include_related_worktrees:
            return _single_report(
                PortableAdoptionStatus.UNSUPPORTED,
                target,
                "Explicit adoption requires an existing canonical Git checkout.",
            )
        request = _request(
            configuration_path,
            configuration_sha256,
            projects=(target,),
            sessions=(),
            unavailable_projects=(target,),
        )
        commit = catalog.adopt_projects(request)
        status = (
            PortableAdoptionStatus.UNAVAILABLE
            if target in commit.adopted_projects
            else PortableAdoptionStatus.ALREADY_ADOPTED
        )
        return _single_report(
            status,
            target,
            "Imported checkout is unavailable; Relink or Forget.",
        )

    git_checkout = find_git_checkout(target) if target.is_dir() else None
    if git_checkout is None or git_checkout.repo_root.resolve() != target:
        return _single_report(
            PortableAdoptionStatus.UNSUPPORTED,
            target,
            "Adoption requires an existing canonical Git checkout.",
        )
    try:
        primary_git_directory = git_common_directory(target)
    except RuntimeError as error:
        return _single_report(
            PortableAdoptionStatus.UNSUPPORTED,
            target,
            f"Adoption requires a usable canonical Git checkout: {error}",
        )

    checkouts = (target,)
    if include_related_worktrees:
        result = run_captured_text(
            ["git", "worktree", "list", "--porcelain"],
            cwd=target,
        )
        if result.returncode != 0:
            return _single_report(
                PortableAdoptionStatus.UNSUPPORTED,
                target,
                f"Related Git worktrees could not be listed: {result.stderr.strip()}",
            )
        listed_checkouts = tuple(
            dict.fromkeys(
                Path(item["path"]).resolve()
                for item in parse_worktree_list(result.stdout)
                if item.get("path")
            )
        )
        if not listed_checkouts or target not in listed_checkouts:
            return _single_report(
                PortableAdoptionStatus.UNSUPPORTED,
                target,
                "Related Git worktree listing did not include the configured target.",
            )
        checkouts = listed_checkouts

    projects: list[Path] = []
    sessions: list[PortableAdoptionSession] = []
    discovered: dict[Path, tuple[PortableAdoptionSession, ...]] = {}
    preliminary: list[PortableAdoptionEntry] = []
    for checkout in checkouts:
        if not checkout.is_dir():
            return _single_report(
                PortableAdoptionStatus.UNSUPPORTED,
                checkout,
                "Related worktree could not be inspected; reconnect it and retry adoption.",
            )
        related_checkout = find_git_checkout(checkout)
        try:
            related_git_directory = git_common_directory(checkout)
        except RuntimeError:
            related_git_directory = None
        if (
            related_checkout is None
            or related_checkout.repo_root.resolve() != checkout
            or related_git_directory != primary_git_directory
        ):
            return _single_report(
                PortableAdoptionStatus.UNSUPPORTED,
                checkout,
                "Related path is not a canonical Git worktree of the configured target.",
            )
        try:
            candidates = tuple(discoverer(checkout))
        except (OSError, RuntimeError, ValueError) as error:
            return _single_report(
                PortableAdoptionStatus.UNSUPPORTED,
                checkout,
                f"Portable workflow discovery failed: {error}",
            )
        candidate_sessions = tuple(
            _catalog_session(checkout, candidate) for candidate in candidates
        )
        discovered[checkout] = candidate_sessions
        if candidate_sessions or (checkout == target and register_primary_without_workflow):
            projects.append(checkout)
            sessions.extend(candidate_sessions)
        else:
            preliminary.append(
                PortableAdoptionEntry(
                    PortableAdoptionStatus.IGNORED,
                    checkout,
                    "Related worktree has no unfinished portable workflow; "
                    "explicit adoption remains available.",
                )
            )

    request = _request(
        configuration_path,
        configuration_sha256,
        projects=tuple(projects),
        sessions=tuple(sessions),
    )
    commit = catalog.adopt_projects(request)
    entries: list[PortableAdoptionEntry] = []
    for checkout in projects:
        checkout_sessions = discovered.get(checkout, ())
        session_ids = tuple(session.session_id for session in checkout_sessions)
        newly_adopted = checkout in commit.adopted_projects or any(
            session_id in commit.adopted_sessions for session_id in session_ids
        )
        entries.append(
            PortableAdoptionEntry(
                (
                    PortableAdoptionStatus.ADOPTED
                    if newly_adopted
                    else PortableAdoptionStatus.ALREADY_ADOPTED
                ),
                checkout,
                (
                    f"Registered {len(session_ids)} unfinished portable workflow(s)."
                    if session_ids
                    else "Registered the selected Git checkout."
                ),
                session_ids,
            )
        )
    entries.extend(preliminary)
    return PortableAdoptionReport(tuple(entries))


def _request(
    configuration_path: Path,
    configuration_sha256: str,
    *,
    projects: tuple[Path, ...],
    sessions: tuple[PortableAdoptionSession, ...],
    unavailable_projects: tuple[Path, ...] = (),
) -> PortableAdoptionRequest:
    receipt_name = (
        f"{V021_SOURCE_VERSION}:{configuration_path.resolve().as_uri()}:"
        f"{configuration_sha256}"
    )
    return PortableAdoptionRequest(
        receipt_id=str(uuid.uuid5(uuid.NAMESPACE_URL, receipt_name)),
        source_version=V021_SOURCE_VERSION,
        configuration_path=configuration_path.resolve(),
        configuration_sha256=configuration_sha256,
        projects=projects,
        sessions=sessions,
        unavailable_projects=unavailable_projects,
    )


def _catalog_session(
    checkout: Path,
    candidate: PortableResumeCandidateSource,
) -> PortableAdoptionSession:
    prd_path = candidate.artifacts.prd_path.resolve()
    return PortableAdoptionSession(
        session_id=str(uuid.uuid5(uuid.NAMESPACE_URL, prd_path.as_uri())),
        checkout=checkout.resolve(),
        prd_path=prd_path,
        issues_index_path=candidate.artifacts.issues_index.resolve(),
        completed_issues=candidate.completed_issues,
        total_issues=candidate.total_issues,
        active_issue=candidate.active_issue,
        active_stage=getattr(candidate, "active_stage", None),
    )


def _default_discoverer(checkout: Path) -> Iterable[PortableResumeCandidateSource]:
    from .interactive_runner import find_resume_candidates

    return find_resume_candidates(checkout)


def _single_report(
    status: PortableAdoptionStatus,
    checkout: Path | None,
    detail: str,
) -> PortableAdoptionReport:
    return PortableAdoptionReport((PortableAdoptionEntry(status, checkout, detail),))
