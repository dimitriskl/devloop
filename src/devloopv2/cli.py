"""Command line entry point for the minimal Dev Loop shell.

There are exactly two things this command does:

``devloop options``
    Open Dev Loop Options, the existing menu that sets each Workflow Step's
    Execution Backend, model, and reasoning effort. Unchanged.

``devloop --prd <file> [...]``
    Open the minimal shell and start delivering that PRD straight away,
    continuing an earlier session for the same PRD if one is recorded.

Arguments are handed to the worker verbatim, so this shares ``devloop``'s
parser instead of restating it.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

from devloop.cli import OPTIONS_COMMAND_ALIASES, build_parser, run_options_command
from devloop.portable_launch_target import delivery_launch_checkout
from devloop.portable_sessions import PortableSessionLaunch, PortableWorkflowOperation

from .runner import run_minimal_shell


def main(argv: list[str] | None = None) -> int:
    raw_arguments = tuple(argv if argv is not None else sys.argv[1:])
    if raw_arguments[:1] and raw_arguments[0] in OPTIONS_COMMAND_ALIASES:
        return run_options_command(raw_arguments[1:])

    parser = build_parser()
    args = parser.parse_args(list(raw_arguments))
    argument_base = Path.cwd().resolve()
    try:
        checkout = delivery_launch_checkout(
            current_checkout=argument_base,
            prd_argument=args.prd,
            issues_argument=args.issues,
            create_worktree=args.create_worktree,
            worktree_path_argument=args.worktree_path,
            dry_run=args.dry_run,
        )
    except ValueError as error:
        parser.error(str(error))

    launch = PortableSessionLaunch(
        session_id=str(uuid.uuid4()),
        checkout=checkout,
        operation=PortableWorkflowOperation.DELIVERY,
        arguments=raw_arguments,
        argument_base=argument_base,
    )
    return run_minimal_shell(
        launch,
        prd_path=Path(args.prd).expanduser().resolve(),
        issues_argument=args.issues,
    )
