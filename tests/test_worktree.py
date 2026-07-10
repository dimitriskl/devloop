from __future__ import annotations

import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from devloop import worktree


class WorktreePromptTests(unittest.TestCase):
    def test_worktree_location_asks_parent_path_then_folder_name(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw)
            with mock.patch.object(
                worktree,
                "read_prompt",
                side_effect=[str(parent), "feature-dev"],
            ):
                result = worktree.ask_worktree_location("Implementation worktree")

        self.assertEqual(result, (parent / "feature-dev").resolve())

    def test_worktree_location_rejects_full_path_as_folder_name(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw)
            other = parent / "wrong"
            with mock.patch.object(
                worktree,
                "read_prompt",
                side_effect=[str(parent), str(other), str(parent), "feature-dev"],
            ):
                with redirect_stderr(StringIO()):
                    result = worktree.ask_worktree_location("Implementation worktree")

        self.assertEqual(result, (parent / "feature-dev").resolve())

    def test_resolve_worktree_sanitizes_non_interactive_branch_name(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "feature-dev"
            with mock.patch.object(worktree, "find_registered_worktree", return_value=None), \
                 mock.patch.object(worktree, "branch_exists", return_value=False), \
                 redirect_stdout(StringIO()) as output:
                result = worktree.resolve_worktree(
                    source_repo=root,
                    create_worktree=True,
                    no_worktree=False,
                    worktree_path=target,
                    branch_name="Reset Queue",
                    interactive=False,
                    dry_run=True,
                )

        self.assertEqual(result.repo_root, target.resolve())
        printed = output.getvalue()
        self.assertIn("Using branch name: Reset-Queue", printed)
        self.assertIn("-b Reset-Queue", printed)

    def test_resolve_worktree_reuses_registered_existing_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "feature-dev"
            with mock.patch.object(
                worktree,
                "find_registered_worktree",
                return_value={"branch": "refs/heads/Reset-Queue"},
            ), mock.patch.object(worktree, "run_captured_text") as run_mock:
                with redirect_stdout(StringIO()) as output:
                    result = worktree.resolve_worktree(
                        source_repo=root,
                        create_worktree=True,
                        no_worktree=False,
                        worktree_path=target,
                        branch_name="Reset Queue",
                        interactive=False,
                        dry_run=False,
                    )

        self.assertEqual(result.repo_root, target.resolve())
        self.assertFalse(result.created)
        self.assertIn("Using existing worktree", output.getvalue())
        run_mock.assert_not_called()

    def test_resolve_worktree_reuses_registered_path_even_when_requested_branch_differs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "feature-dev"
            with mock.patch.object(
                worktree,
                "find_registered_worktree",
                return_value={"branch": "refs/heads/ResetQueue"},
            ), mock.patch.object(worktree, "run_captured_text") as run_mock:
                with redirect_stdout(StringIO()) as output:
                    result = worktree.resolve_worktree(
                        source_repo=root,
                        create_worktree=True,
                        no_worktree=False,
                        worktree_path=target,
                        branch_name="Reset Queue",
                        interactive=False,
                        dry_run=False,
                    )

        self.assertEqual(result.repo_root, target.resolve())
        self.assertFalse(result.created)
        printed = output.getvalue()
        self.assertIn("Using existing worktree", printed)
        self.assertIn("branch ResetQueue; requested Reset-Queue", printed)
        run_mock.assert_not_called()

    def test_resolve_existing_worktree_reuses_git_checkout_on_requested_branch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "feature-dev"
            target.mkdir()
            (target / "README.md").write_text("leftover checkout", encoding="utf-8")
            with mock.patch.object(worktree, "find_registered_worktree", return_value=None), \
                 mock.patch.object(
                     worktree,
                     "run_captured_text",
                     side_effect=[
                         subprocess.CompletedProcess(
                             args=["git", "rev-parse", "--show-toplevel"],
                             returncode=0,
                             stdout=f"{target}\n",
                             stderr="",
                         ),
                         subprocess.CompletedProcess(
                             args=["git", "branch", "--show-current"],
                             returncode=0,
                             stdout="Reset-Queue\n",
                             stderr="",
                         ),
                     ],
                 ):
                with redirect_stdout(StringIO()) as output:
                    result = worktree.resolve_existing_worktree(root, target, "Reset-Queue")

        self.assertEqual(result, target.resolve())
        self.assertIn("Using existing worktree", output.getvalue())

    def test_resolve_existing_worktree_rejects_nonempty_non_git_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "feature-dev"
            target.mkdir()
            (target / "leftover.txt").write_text("not a checkout", encoding="utf-8")
            with mock.patch.object(worktree, "find_registered_worktree", return_value=None), \
                 mock.patch.object(
                     worktree,
                     "run_captured_text",
                     return_value=subprocess.CompletedProcess(
                         args=["git", "rev-parse", "--show-toplevel"],
                         returncode=128,
                         stdout="",
                         stderr="not a git checkout",
                     ),
                 ):
                with self.assertRaisesRegex(RuntimeError, "not an empty folder or Git checkout"):
                    worktree.resolve_existing_worktree(root, target, "Reset-Queue")

    def test_build_worktree_add_command_uses_existing_branch_without_new_branch_flag(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "feature-dev"
            with mock.patch.object(worktree, "branch_exists", return_value=True):
                command = worktree.build_worktree_add_command(root, target, "Reset-Queue")

        self.assertEqual(command, ["git", "worktree", "add", str(target), "Reset-Queue"])


if __name__ == "__main__":
    unittest.main()
