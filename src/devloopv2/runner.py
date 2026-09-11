"""Assemble the supervisor and the minimal shell for one delivery run."""
from __future__ import annotations

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
    )
    shell.run()
    return shell.exit_code
