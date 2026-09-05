from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from devloop import cli, worktree
from devloop.portable_runtime import PortableRuntimeBridge, portable_runtime_session
from devloop.portable_session_catalog import PortableLaunchSettings
from devloop.portable_sessions import PortableWorkflowOperation


class PortableDirectRunTests(unittest.TestCase):
    def test_prd_only_invocation_infers_the_canonical_issue_index(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw)
            self._initialize_repository(repository)
            prd = self._write_prd_package(repository, issue_count=2)
            output = StringIO()

            with (
                portable_runtime_session(PortableRuntimeBridge()),
                mock.patch.dict(
                    os.environ,
                    {
                        "APPDATA": str(repository / "configuration"),
                        "DEVLOOP_UI_MODE": "application",
                    },
                    clear=False,
                ),
                mock.patch(
                    "devloop.worktree.read_prompt",
                    side_effect=AssertionError("default direct run asked about worktrees"),
                ),
                redirect_stdout(output),
            ):
                result = cli.main(
                    [
                        "--prd",
                        str(prd),
                        "--dry-run",
                    ]
                )

        self.assertEqual(result, 0)
        rendered = output.getvalue()
        self.assertIn("Selected issues: 0001, 0002", rendered)
        self.assertIn("0001-development", rendered)
        self.assertIn("0001-security-review", rendered)
        self.assertIn("0001-final-review", rendered)
        self.assertIn("0001-qa", rendered)
        self.assertIn("self-improvement wiki update skipped for dry run", rendered)

    def test_prd_only_invocation_rejects_legacy_issue_index_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw)
            self._initialize_repository(repository)
            package = repository / "prd" / "direct-run"
            package.mkdir(parents=True)
            prd = package / "direct-run.md"
            prd.write_text(
                "# Direct Run\n\n"
                "## Target Product\n\n"
                "Product: devloop-plan + devloop\n",
                encoding="utf-8",
            )
            (package / "README.md").write_text(
                "- [Legacy issue](./0001-legacy.md)\n",
                encoding="utf-8",
            )
            (package / "0001-legacy.md").write_text(
                "# Legacy issue\n\nCompleted: [ ]\n",
                encoding="utf-8",
            )
            error = StringIO()

            with (
                portable_runtime_session(PortableRuntimeBridge()),
                mock.patch.dict(
                    os.environ,
                    {
                        "APPDATA": str(repository / "configuration"),
                        "DEVLOOP_UI_MODE": "application",
                    },
                    clear=False,
                ),
                mock.patch.object(cli, "CodexRunner") as runner_type,
                redirect_stderr(error),
                self.assertRaises(SystemExit) as raised,
            ):
                cli.main(
                    [
                        "--prd",
                        str(prd),
                        "--dry-run",
                        "--no-worktree",
                    ]
                )

        self.assertEqual(raised.exception.code, 2)
        self.assertIn(str(package / "issues" / "README.md"), error.getvalue())
        runner_type.assert_not_called()

    def test_explicit_issue_index_overrides_the_canonical_location(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw)
            self._initialize_repository(repository)
            prd = self._write_prd_package(repository, issue_count=1)
            canonical_issues = prd.parent / "issues"
            alternate_issues = repository / "alternate-issues"
            canonical_issues.rename(alternate_issues)
            output = StringIO()

            with (
                portable_runtime_session(PortableRuntimeBridge()),
                mock.patch.dict(
                    os.environ,
                    {
                        "APPDATA": str(repository / "configuration"),
                        "DEVLOOP_UI_MODE": "application",
                    },
                    clear=False,
                ),
                redirect_stdout(output),
            ):
                result = cli.main(
                    [
                        "--prd",
                        str(prd),
                        "--issues",
                        str(alternate_issues / "README.md"),
                        "--dry-run",
                        "--no-worktree",
                    ]
                )

        self.assertEqual(result, 0)
        self.assertIn("Selected issues: 0001", output.getvalue())

    def test_single_issue_option_limits_a_prd_only_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw)
            self._initialize_repository(repository)
            prd = self._write_prd_package(repository, issue_count=2)
            output = StringIO()

            with (
                portable_runtime_session(PortableRuntimeBridge()),
                mock.patch.dict(
                    os.environ,
                    {
                        "APPDATA": str(repository / "configuration"),
                        "DEVLOOP_UI_MODE": "application",
                    },
                    clear=False,
                ),
                redirect_stdout(output),
            ):
                result = cli.main(
                    [
                        "--prd",
                        str(prd),
                        "--single-issue",
                        "--dry-run",
                    ]
                )

        self.assertEqual(result, 0)
        self.assertIn("Selected issues: 0001", output.getvalue())
        self.assertNotIn("Selected issues: 0001, 0002", output.getvalue())

    def test_start_issue_continues_through_the_remaining_issues_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw)
            self._initialize_repository(repository)
            prd = self._write_prd_package(repository, issue_count=3)
            output = StringIO()

            with (
                portable_runtime_session(PortableRuntimeBridge()),
                mock.patch.dict(
                    os.environ,
                    {
                        "APPDATA": str(repository / "configuration"),
                        "DEVLOOP_UI_MODE": "application",
                    },
                    clear=False,
                ),
                redirect_stdout(output),
            ):
                result = cli.main(
                    [
                        "--prd",
                        str(prd),
                        "--start-issue",
                        "2",
                        "--dry-run",
                    ]
                )

        self.assertEqual(result, 0)
        self.assertIn("Selected issues: 0002, 0003", output.getvalue())

    def test_start_issue_with_single_issue_stops_after_the_match(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw)
            self._initialize_repository(repository)
            prd = self._write_prd_package(repository, issue_count=3)
            output = StringIO()

            with (
                portable_runtime_session(PortableRuntimeBridge()),
                mock.patch.dict(
                    os.environ,
                    {
                        "APPDATA": str(repository / "configuration"),
                        "DEVLOOP_UI_MODE": "application",
                    },
                    clear=False,
                ),
                redirect_stdout(output),
            ):
                result = cli.main(
                    [
                        "--prd",
                        str(prd),
                        "--start-issue",
                        "2",
                        "--single-issue",
                        "--dry-run",
                    ]
                )

        self.assertEqual(result, 0)
        self.assertIn("Selected issues: 0002", output.getvalue())
        self.assertNotIn("Selected issues: 0002, 0003", output.getvalue())

    def test_all_and_single_issue_options_are_mutually_exclusive(self) -> None:
        error = StringIO()

        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            cli.main(
                [
                    "--prd",
                    "prd.md",
                    "--all",
                    "--single-issue",
                ]
            )

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("not allowed with argument --all", error.getvalue())

    def test_omitted_worktree_choice_uses_the_source_checkout_in_every_mode(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw)
            with mock.patch.object(
                worktree,
                "read_prompt",
                side_effect=AssertionError("omitted worktree choice prompted"),
            ):
                interactive = worktree.resolve_worktree(
                    source_repo=source,
                    create_worktree=False,
                    no_worktree=False,
                    worktree_path=None,
                    branch_name=None,
                    interactive=True,
                    dry_run=True,
                )
                non_interactive = worktree.resolve_worktree(
                    source_repo=source,
                    create_worktree=False,
                    no_worktree=False,
                    worktree_path=None,
                    branch_name=None,
                    interactive=False,
                    dry_run=True,
                )

        self.assertEqual(interactive.repo_root, source)
        self.assertEqual(non_interactive.repo_root, source)
        self.assertFalse(interactive.created)
        self.assertFalse(non_interactive.created)

    def test_help_describes_the_prd_only_defaults_and_opt_outs(self) -> None:
        help_text = cli.build_parser().format_help()

        self.assertIn("Default: <PRD folder>/issues/README.md", help_text)
        self.assertIn("Default: presets/generic-minimal.json", help_text)
        self.assertIn("--all", help_text)
        self.assertIn("--single-issue", help_text)
        self.assertIn("--create-worktree", help_text)
        self.assertIn("--no-worktree", help_text)
        self.assertIn("source checkout directly. This is the default", help_text)
        self.assertIn("--self-improvement-wiki", help_text)
        self.assertIn("--no-self-improvement-wiki", help_text)

    def test_portable_session_launch_persists_prd_only_delivery_context(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw)
            self._initialize_repository(repository)
            prd = self._write_prd_package(repository, issue_count=2)

            settings = PortableLaunchSettings.from_arguments(
                ("--prd", str(prd), "--single-issue"),
                operation=PortableWorkflowOperation.DELIVERY,
                checkout=repository,
            )
            arguments = settings.to_arguments(
                checkout=repository,
                prd_path=None,
                issues_index_path=None,
                operation=PortableWorkflowOperation.DELIVERY,
            )

        self.assertEqual(Path(settings.delivery_prd_path or ""), prd.resolve())
        self.assertEqual(
            Path(settings.delivery_issues_path or ""),
            (prd.parent / "issues" / "README.md").resolve(),
        )
        self.assertIn("--single-issue", arguments)

    @staticmethod
    def _initialize_repository(repository: Path) -> None:
        subprocess.run(
            ["git", "init", "--quiet", str(repository)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "config", "core.excludesFile", ""],
            check=True,
            capture_output=True,
        )

    @staticmethod
    def _write_prd_package(repository: Path, *, issue_count: int) -> Path:
        package = repository / "prd" / "direct-run"
        issues = package / "issues"
        issues.mkdir(parents=True)
        prd = package / "direct-run.md"
        prd.write_text(
            "# Direct Run\n\n"
            "## Target Product\n\n"
            "Product: devloop-plan + devloop\n",
            encoding="utf-8",
        )
        links: list[str] = []
        for number in range(1, issue_count + 1):
            issue_name = f"{number:04d}-example.md"
            (issues / issue_name).write_text(
                f"# Issue {number:04d}\n\n"
                "## Target Product\n\n"
                "Product: devloop-plan + devloop\n\n"
                "Completed: [ ]\n\n"
                "## Blocked by\n\n"
                "None.\n",
                encoding="utf-8",
            )
            links.append(f"- [Issue {number:04d}](./{issue_name})\n")
        (issues / "README.md").write_text("".join(links), encoding="utf-8")
        return prd


if __name__ == "__main__":
    unittest.main()
