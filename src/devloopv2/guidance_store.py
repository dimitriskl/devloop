"""Persist operator guidance typed during a run into the active issue's durable replies."""
from __future__ import annotations

from pathlib import Path

from devloop.issue_reply import IssueReply, save_issue_reply

#: ``CodexRunner`` derives its log root from the issue index the same way.
LOG_DIRECTORY_NAME = ".loop.logs"


class NoActiveIssue(RuntimeError):
    """Guidance was typed before the run reported which issue it is working on."""


class GuidanceStore:
    """Write free-text guidance where the agent already looks for operator replies.

    ``devloop.codex_runner`` reads these back through
    ``issue_reply_context()`` when it builds the next prompt for an issue, so
    saving one is all it takes for the guidance to reach the agent on resume.
    """

    def __init__(self, log_root: Path) -> None:
        self._log_root = log_root

    @property
    def log_root(self) -> Path:
        return self._log_root

    def save(self, issue_number: str | None, text: str) -> Path:
        """Record ``text`` against ``issue_number`` and return the saved reply path.

        Raises:
            NoActiveIssue: no issue is in progress yet, so there is nothing to attach to.
            ValueError: the guidance is empty or too long to persist.
        """
        if not issue_number:
            raise NoActiveIssue(
                "No issue is active yet, so there is nothing to attach guidance to."
            )
        self._log_root.mkdir(parents=True, exist_ok=True)
        return save_issue_reply(self._log_root, issue_number, IssueReply(text=text))


def issues_index_path(prd_path: Path, issues_argument: str | None) -> Path:
    """Resolve the issue index the run uses, mirroring the delivery default."""
    if issues_argument:
        return Path(issues_argument).expanduser().resolve()
    return (prd_path.parent / "issues" / "README.md").resolve()


def guidance_store_for(prd_path: Path, issues_argument: str | None) -> GuidanceStore:
    index = issues_index_path(prd_path, issues_argument)
    return GuidanceStore(index.parent / LOG_DIRECTORY_NAME)
