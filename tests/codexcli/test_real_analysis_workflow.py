from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from devloop.application.analysis import AnalysisWorkflowService
from devloop.application.config import ApplicationConfig


@pytest.mark.integration
def test_real_analysis_clarification_resume_and_publication(tmp_path: Path) -> None:
    if os.environ.get("DEVLOOP_REAL_ANALYSIS") != "1":
        pytest.skip("Set DEVLOOP_REAL_ANALYSIS=1 to run the real analysis workflow gate.")
    repository = tmp_path / "project"
    repository.mkdir()
    subprocess.run(["git", "init", str(repository)], check=True, capture_output=True)
    environment = {
        "APPDATA": str(tmp_path / "user-config"),
        "LOCALAPPDATA": str(tmp_path / "user-data"),
    }
    service = AnalysisWorkflowService(
        ApplicationConfig.resolve(repository, environment=environment)
    )

    first = service.start("Build a useful application, but ask me what it should do first.")
    assert first.clarification
    thread_id = first.snapshot.analysis.thread_id
    assert thread_id is not None
    service.pause(first.snapshot.run_id)
    restarted_service = AnalysisWorkflowService(
        ApplicationConfig.resolve(repository, environment=environment)
    )
    assert first.snapshot.run_id in {
        candidate.run_id for candidate in restarted_service.list_resumable()
    }
    resumed = restarted_service.resume(first.snapshot.run_id)
    assert resumed.snapshot.run_id == first.snapshot.run_id
    assert resumed.snapshot.analysis.thread_id == thread_id

    detailed = restarted_service.continue_analysis(
        first.snapshot.run_id,
        "Build a local grocery list price comparison application. Users enter grocery items and "
        "supermarket websites; the system retrieves real public prices responsibly and identifies "
        "the single supermarket with the lowest available total. Produce exactly two "
        "dependency-ordered issues with exactly two acceptance criteria per issue.",
    )
    assert detailed.draft is not None
    assert detailed.findings == ()
    assert detailed.snapshot.analysis.thread_id == thread_id
    acceptance = restarted_service.accept(first.snapshot.run_id)
    package_root = Path(acceptance.package.root)
    assert package_root.is_dir()
    prd_path = package_root / f"{package_root.name}.md"
    issue_set_path = package_root / "issues" / "index.json"
    assert hashlib.sha256(prd_path.read_bytes()).hexdigest() == acceptance.package.prd_hash
    assert (
        hashlib.sha256(issue_set_path.read_bytes()).hexdigest()
        == acceptance.package.issue_set_hash
    )
