"""Assemble the supervisor and the minimal shell for one delivery run."""
from __future__ import annotations

import sys
from pathlib import Path

from devloop.portable_sessions import PortableSessionLaunch

from .guidance_store import guidance_store_for
from .shell import MinimalShell


def run_minimal_shell(
    launch: PortableSessionLaunch,
    *,
    prd_path: Path,
    issues_argument: str | None,
) -> int:
    """Run ``launch`` to completion in the minimal shell and return its exit code."""
    from devloop.interactive_runner import find_resume_candidates
    from devloop.portable_session_catalog import (
        PortableResumeCandidate,
        PortableSessionCatalog,
    )
    from devloop.portable_sessions import PortableSessionSupervisor

    catalog = PortableSessionCatalog()

    def load_resume_candidates() -> tuple[PortableResumeCandidate, ...]:
        return catalog.discover_resume_candidates(find_resume_candidates)

    resume_session_id: str | None = None
    while True:
        supervisor = PortableSessionSupervisor(
            catalog=catalog,
            resume_candidates=load_resume_candidates(),
            resume_candidates_loader=load_resume_candidates,
        )
        shell = MinimalShell(
            supervisor=supervisor,
            launch=launch,
            guidance=guidance_store_for(prd_path, issues_argument),
            prd_path=prd_path,
            resume_session_id=resume_session_id,
        )
        shell.run()
        if shell.launch_error is not None:
            # The shell has already been torn down by now; this is the only place
            # the reason can still reach the operator.
            print(f"Dev Loop could not start: {shell.launch_error}", file=sys.stderr)
        if not shell.options_requested:
            return shell.exit_code
        if shell.options_session_id is None:
            return _report_options_resume_failure("The selected session was not available.")
        resume_session_id = shell.options_session_id

        from devloop.cli import run_options_command

        options_exit_code = run_options_command(())
        if options_exit_code != 0:
            return options_exit_code


def _report_options_resume_failure(message: str) -> int:
    print(f"Dev Loop could not return from Options: {message}", file=sys.stderr)
    return 73
