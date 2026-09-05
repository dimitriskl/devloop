from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from devloop.interactive_runner import find_resume_candidates
from devloop.portable_project_adoption import (
    PortableAdoptionStatus,
    adopt_existing_checkout,
    adopt_v021_configuration,
)
from devloop.portable_session_catalog import (
    PortableResumeCandidateSource,
    PortableSessionCatalog,
    PortableSessionCatalogError,
)
from devloop.portable_sessions import PortableSessionStatus, PortableWorkflowOperation


def _git(checkout: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout


def _initialize_repository(root: Path) -> None:
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "devloop@example.invalid")
    _git(root, "config", "user.name", "Dev Loop Tests")
    (root / "README.md").write_text("# Existing project\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "initial")


def _write_workflow(checkout: Path, name: str = "unfinished") -> tuple[Path, ...]:
    feature = checkout / "prd" / name
    issues = feature / "issues"
    logs = feature / ".loop.logs"
    issues.mkdir(parents=True)
    logs.mkdir()
    prd = feature / f"{name}.md"
    index = issues / "README.md"
    issue = issues / "0001-existing-work.md"
    state = issues / "README.loop.state.json"
    log = logs / "existing.log"
    prd.write_text(f"# {name}\n", encoding="utf-8")
    index.write_text("- [Issue 0001](./0001-existing-work.md)\n", encoding="utf-8")
    issue.write_text("# Existing work\n\nCompleted: [ ]\n", encoding="utf-8")
    state.write_text(
        json.dumps({"issues": {"0001": {"status": "IN_PROGRESS"}}}) + "\n",
        encoding="utf-8",
    )
    log.write_bytes(b"existing log evidence\r\n")
    return prd, index, issue, state, log


def _hashes(paths: tuple[Path, ...]) -> dict[Path, str]:
    return {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


class PortableProjectAdoptionTests(unittest.TestCase):
    def test_empty_successful_worktree_listing_writes_nothing_until_valid_retry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            checkout = root / "project"
            _initialize_repository(checkout)
            _write_workflow(checkout)
            configuration = root / "devloop-plan.json"
            configuration.write_text(
                json.dumps(
                    {
                        "target_repo": str(checkout),
                        "target_repo_confirmed": True,
                    }
                ),
                encoding="utf-8",
            )
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")

            with patch(
                "devloop.portable_project_adoption.run_captured_text",
                return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
            ):
                failed = adopt_v021_configuration(catalog, configuration)

            self.assertEqual(len(failed.entries), 1)
            self.assertEqual(failed.entries[0].status, PortableAdoptionStatus.UNSUPPORTED)
            self.assertEqual(catalog.list_saved_projects(), ())
            self.assertEqual(catalog.list_sessions(), ())
            self.assertEqual(catalog.list_adoption_receipts(), ())

            retry = adopt_v021_configuration(catalog, configuration)

            self.assertEqual(retry.entries[0].status, PortableAdoptionStatus.ADOPTED)
            self.assertEqual(len(catalog.list_saved_projects()), 1)
            self.assertEqual(len(catalog.list_sessions()), 1)
            self.assertEqual(len(catalog.list_adoption_receipts()), 1)

    def test_worktree_listing_omitting_target_writes_nothing_until_valid_retry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            checkout = root / "project"
            omitted_path = root / "other"
            _initialize_repository(checkout)
            _write_workflow(checkout)
            configuration = root / "devloop-plan.json"
            configuration.write_text(
                json.dumps(
                    {
                        "target_repo": str(checkout),
                        "target_repo_confirmed": True,
                    }
                ),
                encoding="utf-8",
            )
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            incomplete_listing = (
                f"worktree {omitted_path}\n"
                "HEAD 0123456789abcdef\n"
                "branch refs/heads/other\n\n"
            )

            with patch(
                "devloop.portable_project_adoption.run_captured_text",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=incomplete_listing,
                    stderr="",
                ),
            ):
                failed = adopt_v021_configuration(catalog, configuration)

            self.assertEqual(len(failed.entries), 1)
            self.assertEqual(failed.entries[0].status, PortableAdoptionStatus.UNSUPPORTED)
            self.assertEqual(failed.entries[0].checkout, checkout.resolve())
            self.assertEqual(catalog.list_saved_projects(), ())
            self.assertEqual(catalog.list_sessions(), ())
            self.assertEqual(catalog.list_adoption_receipts(), ())

            retry = adopt_v021_configuration(catalog, configuration)

            self.assertEqual(retry.entries[0].status, PortableAdoptionStatus.ADOPTED)
            self.assertEqual(len(catalog.list_saved_projects()), 1)
            self.assertEqual(len(catalog.list_sessions()), 1)
            self.assertEqual(len(catalog.list_adoption_receipts()), 1)

    def test_v021_configuration_adopts_only_target_and_related_unfinished_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            unfinished = root / "unfinished-worktree"
            artifact_free = root / "artifact-free-worktree"
            _initialize_repository(source)
            _git(source, "worktree", "add", "-b", "unfinished", str(unfinished))
            _git(source, "worktree", "add", "-b", "artifact-free", str(artifact_free))
            first_workflow = _write_workflow(unfinished)
            second_workflow = _write_workflow(unfinished, "also-unfinished")
            artifacts = first_workflow + second_workflow
            before_hashes = _hashes(artifacts)
            before_worktrees = _git(source, "worktree", "list", "--porcelain")
            before_branches = _git(source, "branch", "--format=%(refname)")
            configuration = root / "devloop-plan.json"
            configuration.write_text(
                json.dumps(
                    {
                        "target_repo": str(source),
                        "target_repo_confirmed": True,
                        "selection": {"reasoning": "high"},
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            before_configuration = configuration.read_bytes()
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")

            first = adopt_v021_configuration(catalog, configuration)
            second = adopt_v021_configuration(catalog, configuration)

            projects = catalog.list_saved_projects()
            sessions = catalog.list_sessions()
            receipts = catalog.list_adoption_receipts()
            self.assertEqual(
                {project.checkout for project in projects},
                {source.resolve(), unfinished.resolve()},
            )
            self.assertNotIn(artifact_free.resolve(), {project.checkout for project in projects})
            self.assertEqual(len(sessions), 2)
            session = next(
                record for record in sessions if record.prd_path == artifacts[0].resolve()
            )
            self.assertEqual(
                session.session_id,
                str(uuid.uuid5(uuid.NAMESPACE_URL, artifacts[0].resolve().as_uri())),
            )
            self.assertEqual(session.checkout, unfinished.resolve())
            self.assertEqual(session.operation, PortableWorkflowOperation.PLANNING)
            self.assertEqual(session.status, PortableSessionStatus.READY)
            self.assertEqual(session.issues_index_path, artifacts[1].resolve())
            self.assertEqual(session.progress.active_issue, "0001")
            self.assertEqual(session.progress.total_issues, 1)
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0].source_version, "0.2.1")
            self.assertEqual(receipts[0].configuration_path, configuration.resolve())
            self.assertEqual(
                {entry.status for entry in first.entries},
                {PortableAdoptionStatus.ADOPTED, PortableAdoptionStatus.IGNORED},
            )
            self.assertEqual(
                {entry.status for entry in second.entries},
                {PortableAdoptionStatus.ALREADY_ADOPTED, PortableAdoptionStatus.IGNORED},
            )
            self.assertIn("adopted", first.render().lower())
            self.assertIn("already adopted", second.render().lower())
            self.assertIn("ignored", first.render().lower())
            self.assertEqual(configuration.read_bytes(), before_configuration)
            self.assertEqual(_hashes(artifacts), before_hashes)
            self.assertEqual(_git(source, "worktree", "list", "--porcelain"), before_worktrees)
            self.assertEqual(_git(source, "branch", "--format=%(refname)"), before_branches)
            self.assertIsNone(catalog.get_worktree_lease(source))
            self.assertIsNone(catalog.get_worktree_lease(unfinished))

    def test_unavailable_target_is_actionable_and_does_not_block_valid_adoption(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            unavailable = root / "disconnected" / "project"
            configuration = root / "devloop-plan.json"
            configuration.write_text(
                json.dumps(
                    {
                        "target_repo": str(unavailable),
                        "target_repo_confirmed": True,
                    }
                ),
                encoding="utf-8",
            )
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")

            unavailable_report = adopt_v021_configuration(catalog, configuration)

            self.assertEqual(
                unavailable_report.entries[0].status,
                PortableAdoptionStatus.UNAVAILABLE,
            )
            unavailable_session = catalog.list_sessions()[0]
            self.assertEqual(unavailable_session.checkout, unavailable.resolve())
            self.assertEqual(unavailable_session.status, PortableSessionStatus.UNAVAILABLE)
            self.assertIn("Relink or Forget", unavailable_session.activity_summary)

            available = root / "available"
            _initialize_repository(available)
            artifacts = _write_workflow(available)
            before = _hashes(artifacts)
            available_report = adopt_existing_checkout(catalog, available)

            self.assertEqual(available_report.entries[0].status, PortableAdoptionStatus.ADOPTED)
            self.assertEqual(len(catalog.list_sessions()), 2)
            self.assertEqual(_hashes(artifacts), before)
            catalog.forget_session(unavailable_session.session_id)
            self.assertEqual(len(catalog.list_sessions()), 1)

    def test_explicit_adoption_registers_an_artifact_free_existing_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            checkout = root / "additional"
            _initialize_repository(checkout)
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")

            first = adopt_existing_checkout(catalog, checkout)
            second = adopt_existing_checkout(catalog, checkout)

            self.assertEqual(first.entries[0].status, PortableAdoptionStatus.ADOPTED)
            self.assertEqual(second.entries[0].status, PortableAdoptionStatus.ALREADY_ADOPTED)
            self.assertEqual(
                [project.checkout for project in catalog.list_saved_projects()],
                [checkout.resolve()],
            )
            self.assertEqual(catalog.list_sessions(), ())
            self.assertEqual(len(catalog.list_adoption_receipts()), 1)

    def test_unsupported_configuration_is_reported_without_a_false_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            configuration = root / "devloop-plan.json"
            configuration.write_text(
                '{"target_repo": 42, "target_repo_confirmed": true}',
                encoding="utf-8",
            )
            before = configuration.read_bytes()
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")

            report = adopt_v021_configuration(catalog, configuration)

            self.assertEqual(report.entries[0].status, PortableAdoptionStatus.UNSUPPORTED)
            self.assertIn("unsupported", report.render().lower())
            self.assertEqual(catalog.list_saved_projects(), ())
            self.assertEqual(catalog.list_adoption_receipts(), ())
            self.assertEqual(configuration.read_bytes(), before)

    def test_primary_discovery_failure_writes_nothing_and_corrected_retry_adopts_once(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            checkout = root / "project"
            _initialize_repository(checkout)
            artifacts = _write_workflow(checkout)
            before = _hashes(artifacts)
            configuration = root / "devloop-plan.json"
            configuration.write_text(
                json.dumps(
                    {
                        "target_repo": str(checkout),
                        "target_repo_confirmed": True,
                    }
                ),
                encoding="utf-8",
            )
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")

            def fail_discovery(
                _checkout: Path,
            ) -> tuple[PortableResumeCandidateSource, ...]:
                raise RuntimeError("discovery unavailable")

            failed = adopt_v021_configuration(
                catalog,
                configuration,
                discoverer=fail_discovery,
            )

            self.assertEqual(len(failed.entries), 1)
            self.assertEqual(failed.entries[0].status, PortableAdoptionStatus.UNSUPPORTED)
            self.assertIn("discovery unavailable", failed.entries[0].detail)
            self.assertEqual(catalog.list_saved_projects(), ())
            self.assertEqual(catalog.list_sessions(), ())
            self.assertEqual(catalog.list_adoption_receipts(), ())

            retry = adopt_v021_configuration(catalog, configuration)

            self.assertEqual(retry.entries[0].status, PortableAdoptionStatus.ADOPTED)
            self.assertEqual(len(catalog.list_saved_projects()), 1)
            self.assertEqual(len(catalog.list_sessions()), 1)
            self.assertEqual(len(catalog.list_adoption_receipts()), 1)
            self.assertEqual(_hashes(artifacts), before)

    def test_present_non_checkout_targets_are_unsupported_without_catalog_writes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repository = root / "repository"
            _initialize_repository(repository)
            nested = repository / "nested"
            nested.mkdir()
            non_git = root / "non-git"
            non_git.mkdir()
            file_target = root / "not-a-directory"
            file_target.write_text("present\n", encoding="utf-8")

            for index, target in enumerate((nested, non_git, file_target), start=1):
                with self.subTest(target=target):
                    configuration = root / f"devloop-plan-{index}.json"
                    configuration.write_text(
                        json.dumps(
                            {
                                "target_repo": str(target),
                                "target_repo_confirmed": True,
                            }
                        ),
                        encoding="utf-8",
                    )
                    catalog = PortableSessionCatalog(root / f"catalog-{index}.sqlite3")

                    report = adopt_v021_configuration(catalog, configuration)

                    self.assertEqual(len(report.entries), 1)
                    self.assertEqual(
                        report.entries[0].status,
                        PortableAdoptionStatus.UNSUPPORTED,
                    )
                    self.assertNotIn("Relink or Forget", report.entries[0].detail)
                    self.assertEqual(catalog.list_saved_projects(), ())
                    self.assertEqual(catalog.list_sessions(), ())
                    self.assertEqual(catalog.list_adoption_receipts(), ())

    def test_related_worktree_discovery_failure_aborts_the_whole_adoption(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            related = root / "related"
            _initialize_repository(source)
            _git(source, "worktree", "add", "-b", "related", str(related))
            artifacts = _write_workflow(related)
            before = _hashes(artifacts)
            configuration = root / "devloop-plan.json"
            configuration.write_text(
                json.dumps(
                    {
                        "target_repo": str(source),
                        "target_repo_confirmed": True,
                    }
                ),
                encoding="utf-8",
            )
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")

            def discover(checkout: Path) -> tuple[PortableResumeCandidateSource, ...]:
                if checkout.resolve() == related.resolve():
                    raise RuntimeError("related discovery unavailable")
                return tuple(find_resume_candidates(checkout))

            failed = adopt_v021_configuration(
                catalog,
                configuration,
                discoverer=discover,
            )

            self.assertEqual(len(failed.entries), 1)
            self.assertEqual(failed.entries[0].status, PortableAdoptionStatus.UNSUPPORTED)
            self.assertEqual(failed.entries[0].checkout, related.resolve())
            self.assertIn("related discovery unavailable", failed.entries[0].detail)
            self.assertEqual(catalog.list_saved_projects(), ())
            self.assertEqual(catalog.list_sessions(), ())
            self.assertEqual(catalog.list_adoption_receipts(), ())
            self.assertEqual(_hashes(artifacts), before)

            retry = adopt_v021_configuration(catalog, configuration)

            self.assertEqual(
                {entry.status for entry in retry.entries},
                {PortableAdoptionStatus.ADOPTED},
            )
            self.assertEqual(len(catalog.list_saved_projects()), 2)
            self.assertEqual(len(catalog.list_sessions()), 1)
            self.assertEqual(len(catalog.list_adoption_receipts()), 1)
            self.assertEqual(_hashes(artifacts), before)

    def test_present_related_path_must_still_be_the_registered_git_worktree(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            related = root / "related"
            _initialize_repository(source)
            _git(source, "worktree", "add", "-b", "related-invalid", str(related))
            artifacts = _write_workflow(related)
            before = _hashes(artifacts)
            (related / ".git").unlink()
            configuration = root / "devloop-plan.json"
            configuration.write_text(
                json.dumps(
                    {
                        "target_repo": str(source),
                        "target_repo_confirmed": True,
                    }
                ),
                encoding="utf-8",
            )
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")

            report = adopt_v021_configuration(catalog, configuration)

            self.assertEqual(len(report.entries), 1)
            self.assertEqual(report.entries[0].status, PortableAdoptionStatus.UNSUPPORTED)
            self.assertEqual(report.entries[0].checkout, related.resolve())
            self.assertIn("canonical Git worktree", report.entries[0].detail)
            self.assertEqual(catalog.list_saved_projects(), ())
            self.assertEqual(catalog.list_sessions(), ())
            self.assertEqual(catalog.list_adoption_receipts(), ())
            self.assertEqual(_hashes(artifacts), before)

    def test_disconnected_related_worktree_aborts_until_discovery_is_complete(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            related = root / "related"
            disconnected = root / "related-disconnected"
            _initialize_repository(source)
            _git(source, "worktree", "add", "-b", "related-offline", str(related))
            artifacts = _write_workflow(related)
            relative_artifacts = tuple(path.relative_to(related) for path in artifacts)
            before = {
                path: hashlib.sha256((related / path).read_bytes()).hexdigest()
                for path in relative_artifacts
            }
            related.rename(disconnected)
            configuration = root / "devloop-plan.json"
            configuration.write_text(
                json.dumps(
                    {
                        "target_repo": str(source),
                        "target_repo_confirmed": True,
                    }
                ),
                encoding="utf-8",
            )
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")

            failed = adopt_v021_configuration(catalog, configuration)

            self.assertEqual(len(failed.entries), 1)
            self.assertEqual(failed.entries[0].status, PortableAdoptionStatus.UNSUPPORTED)
            self.assertEqual(failed.entries[0].checkout, related.resolve())
            self.assertIn("could not be inspected", failed.entries[0].detail)
            self.assertEqual(catalog.list_saved_projects(), ())
            self.assertEqual(catalog.list_sessions(), ())
            self.assertEqual(catalog.list_adoption_receipts(), ())
            self.assertEqual(
                {
                    path: hashlib.sha256((disconnected / path).read_bytes()).hexdigest()
                    for path in relative_artifacts
                },
                before,
            )

            disconnected.rename(related)
            retry = adopt_v021_configuration(catalog, configuration)

            self.assertEqual(
                {entry.status for entry in retry.entries},
                {PortableAdoptionStatus.ADOPTED},
            )
            self.assertEqual(len(catalog.list_saved_projects()), 2)
            self.assertEqual(len(catalog.list_sessions()), 1)
            self.assertEqual(len(catalog.list_adoption_receipts()), 1)
            self.assertEqual(
                {
                    path: hashlib.sha256((related / path).read_bytes()).hexdigest()
                    for path in relative_artifacts
                },
                before,
            )

    def test_catalog_transaction_rolls_back_before_receipt_and_preserves_old_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            checkout = root / "project"
            _initialize_repository(checkout)
            artifacts = _write_workflow(checkout)
            before = _hashes(artifacts)
            valid = find_resume_candidates(checkout)[0]
            invalid = SimpleNamespace(
                artifacts=valid.artifacts,
                completed_issues=0,
                pending_issues=1,
                total_issues=1,
                active_issue="x" * 129,
                active_status="IN_PROGRESS",
                active_stage=None,
                updated_at=valid.updated_at,
            )
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")

            with self.assertRaisesRegex(PortableSessionCatalogError, "write failed"):
                adopt_existing_checkout(catalog, checkout, discoverer=lambda _checkout: (invalid,))

            self.assertEqual(catalog.list_saved_projects(), ())
            self.assertEqual(catalog.list_sessions(), ())
            self.assertEqual(catalog.list_adoption_receipts(), ())
            self.assertEqual(_hashes(artifacts), before)

            retry = adopt_existing_checkout(catalog, checkout)

            self.assertEqual(retry.entries[0].status, PortableAdoptionStatus.ADOPTED)
            self.assertEqual(len(catalog.list_adoption_receipts()), 1)
            self.assertEqual(_hashes(artifacts), before)
            older_runner_candidates = find_resume_candidates(checkout)
            self.assertEqual(len(older_runner_candidates), 1)
            self.assertEqual(older_runner_candidates[0].artifacts.prd_path, artifacts[0].resolve())

            with closing(sqlite3.connect(catalog.path)) as connection:
                counts = {
                    table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in (
                        "saved_projects",
                        "sessions",
                        "worktree_leases",
                        "project_adoption_receipts",
                    )
                }
            self.assertEqual(
                counts,
                {
                    "saved_projects": 1,
                    "sessions": 1,
                    "worktree_leases": 0,
                    "project_adoption_receipts": 1,
                },
            )


if __name__ == "__main__":
    unittest.main()
