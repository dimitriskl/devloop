"""Turn a sequence of session snapshots into the lines that are new since the last one."""
from __future__ import annotations

from dataclasses import dataclass, field

from devloop.portable_sessions import PortableSessionSnapshot, PortableSessionStatus

#: Statuses whose arrival is worth announcing on its own line.
_ANNOUNCED_STATUSES = {
    PortableSessionStatus.QUEUED: "Queued, waiting for a free execution slot.",
    PortableSessionStatus.PAUSING: "Pausing at the next durable checkpoint...",
    PortableSessionStatus.PAUSED: "Paused. Type guidance and press Enter, or press Enter to continue.",
    PortableSessionStatus.INTERRUPTED: "Interrupted. Press Enter to continue from the last checkpoint.",
    PortableSessionStatus.UNAVAILABLE: "Session worktree is unavailable.",
}


OUTPUT = "output"
NOTICE = "notice"
PROBLEM = "problem"


@dataclass(frozen=True)
class ActivityLine:
    """One renderable line plus the intent behind it."""

    text: str
    kind: str = OUTPUT


@dataclass
class ActivityStream:
    """Diff consecutive snapshots of one session into newly displayable lines.

    ``PortableSessionSnapshot.activity`` and ``.diagnostics`` are capped at
    their last 100 entries, so a positional cursor stops being meaningful once
    a busy run rolls past that cap. This tracks the previously seen tuples and
    recovers the new tail by overlap instead.
    """

    _activity: tuple[str, ...] = field(default=(), init=False)
    _diagnostics: tuple[str, ...] = field(default=(), init=False)
    _status: PortableSessionStatus | None = field(default=None, init=False)
    _stage: str = field(default="", init=False)

    def consume(self, snapshot: PortableSessionSnapshot) -> tuple[ActivityLine, ...]:
        lines: list[ActivityLine] = []
        for entry in unseen_tail(self._activity, snapshot.activity):
            for text in entry.splitlines() or [""]:
                lines.append(ActivityLine(text, OUTPUT))
        self._activity = snapshot.activity
        for entry in unseen_tail(self._diagnostics, snapshot.diagnostics):
            for text in entry.splitlines() or [""]:
                lines.append(ActivityLine(text, PROBLEM))
        self._diagnostics = snapshot.diagnostics
        lines.extend(self._stage_lines(snapshot))
        lines.extend(self._status_lines(snapshot))
        return tuple(lines)

    def _stage_lines(self, snapshot: PortableSessionSnapshot) -> tuple[ActivityLine, ...]:
        stage = snapshot.progress.stage.strip()
        if not stage or stage == self._stage:
            return ()
        self._stage = stage
        issue = snapshot.progress.active_issue
        label = f"{stage} - issue {issue}" if issue else stage
        return (ActivityLine(label, NOTICE),)

    def _status_lines(self, snapshot: PortableSessionSnapshot) -> tuple[ActivityLine, ...]:
        if snapshot.status is self._status:
            return ()
        self._status = snapshot.status
        announcement = _ANNOUNCED_STATUSES.get(snapshot.status)
        if announcement is None:
            return ()
        kind = PROBLEM if snapshot.status is PortableSessionStatus.UNAVAILABLE else NOTICE
        return (ActivityLine(announcement, kind),)


def unseen_tail(previous: tuple[str, ...], current: tuple[str, ...]) -> tuple[str, ...]:
    """Return the entries of ``current`` that were not already in ``previous``.

    Both tuples only ever grow at the end and are truncated from the front, so
    the new entries are whatever follows the longest suffix of ``previous``
    that still opens ``current``.
    """
    if not previous:
        return current
    for size in range(min(len(previous), len(current)), 0, -1):
        if previous[-size:] == current[:size]:
            return current[size:]
    return current
