from __future__ import annotations

from pathlib import Path

from devloop.infrastructure.windows_acl import is_safe_windows_acl_grant


def is_safe_development_command(
    command: str,
    workspace: Path,
    sid: str,
) -> bool:
    """Allow only the exact Windows workspace ACL handoff operation.

    Inspection and test commands remain subject to the App Server's typed user
    approval boundary. Treating their command lines as intrinsically safe would
    bypass sandbox prompts and inherited tool configuration.
    """
    return is_safe_windows_acl_grant(command, workspace, sid)
