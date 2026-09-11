"""Decide whether a launch continues an existing session for the same PRD or starts a new one."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from devloop.portable_sessions import PortableSessionSnapshot, PortableSessionStatus

#: Statuses a session can be picked up from without operator choices.
RESUMABLE_STATUSES = frozenset(
    {
        PortableSessionStatus.READY,
        PortableSessionStatus.PAUSED,
        PortableSessionStatus.INTERRUPTED,
    }
)


@dataclass(frozen=True)
class SessionTarget:
    """The session this launch drives, and whether it already has work behind it."""

    session_id: str
    resume: bool


def resolve_session_target(
    snapshots: Iterable[PortableSessionSnapshot],
    *,
    new_session_id: str,
    prd_path: Path,
) -> SessionTarget:
    """Continue the newest resumable session recorded for ``prd_path``, else start fresh.

    Relaunching against the same PRD is the operator's way of saying "carry on
    from where this stopped", so no picker is involved: the catalog already
    knows where each PRD's session left off.
    """
    wanted = _canonical(prd_path)
    resumable = [
        snapshot
        for snapshot in snapshots
        if snapshot.prd_path is not None
        and _canonical(snapshot.prd_path) == wanted
        and _is_resumable(snapshot)
    ]
    if not resumable:
        return SessionTarget(new_session_id, resume=False)
    newest = max(resumable, key=lambda snapshot: snapshot.updated_at)
    return SessionTarget(newest.session_id, resume=True)


def _is_resumable(snapshot: PortableSessionSnapshot) -> bool:
    if snapshot.status in RESUMABLE_STATUSES:
        return True
    # A RUNNING session whose supervisor died is adoptable; the supervisor
    # turns it into a durable checkpoint before continuing it.
    return (
        snapshot.status is PortableSessionStatus.RUNNING and snapshot.recovery_available
    )


def _canonical(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except OSError:
        return path
