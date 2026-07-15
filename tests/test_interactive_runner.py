from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from devloop import interactive_runner
from devloop.interactive_runner import HandoffParams, PlanningArtifacts


class BuildPlanningPromptTests(unittest.TestCase):
    def make_prompt(self) -> str:
        return interactive_runner.build_planning_prompt(
            repo_root=Path("C:/repo"),
            bundle_root=Path("F:/devloop"),
            goal="add login",
            skill_paths=[
                Path("F:/devloop/skills/codex/grill-with-docs/SKILL.md"),
                Path("F:/devloop/skills/codex/to-prd/SKILL.md"),
            ],
            wiki_index=Path("F:/devloop/docs/devloop-self-improvement/wiki/index.md"),
        )

    def test_lists_selected_skills(self) -> None:
        prompt = self.make_prompt()
        self.assertIn("grill-with-docs", prompt)
        self.assertIn("to-prd", prompt)

    def test_references_wiki_index(self) -> None:
        prompt = self.make_prompt()
        self.assertIn("self-improvement wiki", prompt.lower())
        self.assertIn("index.md", prompt)

    def test_never_asks_user_to_exit(self) -> None:
        prompt = self.make_prompt().lower()
        self.assertNotIn("/quit", prompt)
        self.assertNotIn("ctrl+c", prompt)
        self.assertIn("continues automatically", prompt)

    def test_includes_issue_self_containment_rules(self) -> None:
        prompt = self.make_prompt().lower()
        self.assertIn("self-contained", prompt)
        self.assertIn("fresh codex session", prompt)
        self.assertIn("context window", prompt)

    def test_includes_goal(self) -> None:
        self.assertIn("add login", self.make_prompt())

    def test_settled_existing_analysis_advances_to_prd_and_issues(self) -> None:
        prompt = self.make_prompt().lower()
        self.assertIn("existing analysis is already settled", prompt)
        self.assertIn("move directly to $to-prd and then $to-issues", prompt)
        self.assertIn("do not repeat the interview", prompt)

    def test_names_the_active_product_and_excludes_codexcli_by_default(self) -> None:
        prompt = self.make_prompt().lower()
        self.assertIn("target product: devloop-plan + devloop", prompt)
        self.assertIn("codexcli is a separate application", prompt)
        self.assertIn("do not target codexcli", prompt)


class BuildDevloopArgsTests(unittest.TestCase):
    def make_artifacts(self, root: Path) -> PlanningArtifacts:
        prd = root / "prd" / "feature" / "feature.md"
        prd.parent.mkdir(parents=True)
        prd.write_text("prd", encoding="utf-8")
        issues = root / "prd" / "feature" / "issues" / "README.md"
        issues.parent.mkdir(parents=True)
        issues.write_text("issues", encoding="utf-8")
        return PlanningArtifacts(prd_path=prd, issues_index=issues)

    def test_default_params_run_all_with_worktree_and_wiki(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = self.make_artifacts(root)
            params = HandoffParams(
                start_issue=None,
                run_all=True,
                use_worktree=True,
                worktree_path=root / "feature-dev",
                branch_name="devloop/feature",
            )
            args = interactive_runner.build_devloop_args(params, artifacts, None)
        self.assertIn("--all", args)
        self.assertIn("--self-improvement-wiki", args)
        self.assertIn("--create-worktree", args)
        self.assertIn("--branch-name", args)
        self.assertNotIn("--no-self-improvement-wiki", args)
        self.assertNotIn("--preset", args)

    def test_start_issue_and_no_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = self.make_artifacts(root)
            params = HandoffParams(
                start_issue="0002",
                run_all=False,
                use_worktree=False,
                worktree_path=root / "unused",
                branch_name="unused",
            )
            args = interactive_runner.build_devloop_args(params, artifacts, None)
        self.assertIn("--start-issue", args)
        self.assertIn("0002", args)
        self.assertIn("--no-worktree", args)
        self.assertNotIn("--all", args)

    def test_handoff_summary_reports_pending_issue_count(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = self.make_artifacts(root)
            issue_1 = artifacts.issues_index.parent / "0001-first.md"
            issue_2 = artifacts.issues_index.parent / "0002-second.md"
            issue_1.write_text("# First\n\nCompleted: [ ]\n", encoding="utf-8")
            issue_2.write_text("# Second\n\nCompleted: [x]\n", encoding="utf-8")
            artifacts.issues_index.write_text(
                "- [Issue 0001](./0001-first.md)\n"
                "- [Issue 0002](./0002-second.md)\n",
                encoding="utf-8",
            )
            params = HandoffParams(
                start_issue=None,
                run_all=True,
                use_worktree=False,
                worktree_path=root / "unused",
                branch_name="unused",
            )

            summary = interactive_runner.handoff_issue_summary(params, artifacts)

        self.assertEqual(summary, "1 pending")

    def test_session_preset_added_when_provided(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = self.make_artifacts(root)
            preset = root / "session.preset.json"
            preset.write_text("{}", encoding="utf-8")
            params = HandoffParams(
                start_issue=None,
                run_all=True,
                use_worktree=False,
                worktree_path=root / "unused",
                branch_name="unused",
            )
            args = interactive_runner.build_devloop_args(params, artifacts, preset)
        self.assertIn("--preset", args)
        self.assertIn(str(preset), args)


class WorktreePromptTests(unittest.TestCase):
    def test_worktree_location_asks_parent_path_then_folder_name(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw)
            with mock.patch.object(
                interactive_runner,
                "read_prompt",
                side_effect=[str(parent), "feature-dev"],
            ):
                result = interactive_runner.ask_worktree_location("Implementation worktree")

        self.assertEqual(result, (parent / "feature-dev").resolve())

    def test_worktree_location_keeps_default_parent_and_name(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            default = Path(raw) / "feature-dev"
            with mock.patch.object(
                interactive_runner,
                "read_prompt",
                side_effect=["", ""],
            ):
                result = interactive_runner.ask_worktree_location(
                    "Implementation worktree",
                    default=default,
                )

        self.assertEqual(result, default.resolve())

    def test_worktree_location_can_default_parent_without_default_name(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw)
            with mock.patch.object(
                interactive_runner,
                "read_prompt",
                side_effect=["", "feature-dev"],
            ):
                result = interactive_runner.ask_worktree_location(
                    "New worktree",
                    default_parent=parent,
                )

        self.assertEqual(result, (parent / "feature-dev").resolve())

    def test_worktree_location_remembers_parent_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_path = root / "devloop-plan.json"
            parent = root / "worktrees"
            with mock.patch.object(interactive_runner, "plan_state_path", return_value=state_path), \
                 mock.patch.object(
                     interactive_runner,
                     "read_prompt",
                     side_effect=[str(parent), "feature-dev"],
                 ):
                result = interactive_runner.ask_worktree_location(
                    "New worktree",
                    remember_parent=True,
                )
                restored = interactive_runner.load_last_worktree_parent()

        self.assertEqual(result, (parent / "feature-dev").resolve())
        self.assertEqual(restored, parent.resolve())

    def test_branch_name_accepts_human_text_and_sanitizes_for_git(self) -> None:
        with mock.patch.object(
            interactive_runner,
            "read_prompt",
            return_value="Reset Queue",
        ):
            with redirect_stdout(StringIO()) as output:
                result = interactive_runner.ask_branch_name("New worktree branch name")

        self.assertEqual(result, "Reset-Queue")
        self.assertIn("Using branch name: Reset-Queue", output.getvalue())

    def test_create_or_reuse_worktree_reuses_existing_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "feature-dev"
            with mock.patch.object(
                interactive_runner,
                "resolve_existing_worktree",
                return_value=target.resolve(),
            ), mock.patch.object(interactive_runner, "run_git") as run_git:
                result = interactive_runner.create_or_reuse_worktree(root, target, "Reset-Queue")

        self.assertEqual(result, target.resolve())
        run_git.assert_not_called()

    def test_create_or_reuse_worktree_uses_existing_branch_command(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "feature-dev"
            command = ["git", "worktree", "add", str(target), "Reset-Queue"]
            with mock.patch.object(interactive_runner, "resolve_existing_worktree", return_value=None), \
                 mock.patch.object(
                     interactive_runner,
                     "build_worktree_add_command",
                     return_value=command,
                 ), \
                 mock.patch.object(interactive_runner, "run_git") as run_git:
                result = interactive_runner.create_or_reuse_worktree(root, target, "Reset-Queue")

        self.assertEqual(result, target.resolve())
        run_git.assert_called_once_with(command[1:], cwd=root)


class BranchStrategyTests(unittest.TestCase):
    def test_existing_current_branch_is_reused(self) -> None:
        repo_root = Path("C:/repo")
        with mock.patch.object(
            interactive_runner,
            "current_branch",
            return_value="Basic-analysis",
        ), mock.patch.object(
            interactive_runner,
            "ask_choice",
            return_value="2",
        ), mock.patch.object(
            interactive_runner,
            "ask_branch_name",
            return_value="Basic-analysis",
        ), mock.patch.object(
            interactive_runner,
            "run_git",
        ) as run_git, redirect_stdout(StringIO()) as output:
            result = interactive_runner.apply_branch_strategy(repo_root)

        self.assertEqual(result, repo_root)
        run_git.assert_not_called()
        self.assertIn("Using existing branch: Basic-analysis", output.getvalue())

    def test_existing_other_branch_is_checked_out_without_create_flag(self) -> None:
        repo_root = Path("C:/repo")
        with mock.patch.object(
            interactive_runner,
            "current_branch",
            return_value="main",
        ), mock.patch.object(
            interactive_runner,
            "ask_choice",
            return_value="2",
        ), mock.patch.object(
            interactive_runner,
            "ask_branch_name",
            return_value="Basic-analysis",
        ), mock.patch.object(
            interactive_runner,
            "branch_exists",
            return_value=True,
        ), mock.patch.object(
            interactive_runner,
            "run_git",
        ) as run_git, redirect_stdout(StringIO()) as output:
            result = interactive_runner.apply_branch_strategy(repo_root)

        self.assertEqual(result, repo_root)
        run_git.assert_called_once_with(["checkout", "Basic-analysis"], cwd=repo_root)
        self.assertIn("Using existing branch: Basic-analysis", output.getvalue())

    def test_missing_branch_is_created(self) -> None:
        repo_root = Path("C:/repo")
        with mock.patch.object(
            interactive_runner,
            "current_branch",
            return_value="main",
        ), mock.patch.object(
            interactive_runner,
            "ask_choice",
            return_value="2",
        ), mock.patch.object(
            interactive_runner,
            "ask_branch_name",
            return_value="New-analysis",
        ), mock.patch.object(
            interactive_runner,
            "branch_exists",
            return_value=False,
        ), mock.patch.object(
            interactive_runner,
            "run_git",
        ) as run_git:
            result = interactive_runner.apply_branch_strategy(repo_root)

        self.assertEqual(result, repo_root)
        run_git.assert_called_once_with(
            ["checkout", "-b", "New-analysis"],
            cwd=repo_root,
        )


class PlanStateTests(unittest.TestCase):
    def test_save_last_target_repo_preserves_selection_and_worktree_parent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_path = root / "devloop-plan.json"
            repo = root / "repo"
            repo.mkdir()
            state_path.write_text(
                json.dumps(
                    {
                        "selection": {"planning_skills": ["grill-with-docs"]},
                        "last_worktree_parent": str(root / "worktrees"),
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(interactive_runner, "plan_state_path", return_value=state_path):
                interactive_runner.save_last_target_repo(repo)

            data = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(data["target_repo"], str(repo))
        self.assertEqual(data["selection"], {"planning_skills": ["grill-with-docs"]})
        self.assertEqual(data["last_worktree_parent"], str(root / "worktrees"))

    def test_default_worktree_path_uses_remembered_parent_when_supplied(self) -> None:
        root = Path("E:/LocalCode/eConnectorV2")
        parent = Path("E:/Worktrees")
        self.assertEqual(
            interactive_runner.default_worktree_path(root, "reset-queue", parent=parent),
            parent / "eConnectorV2-reset-queue-dev",
        )


class ResumePlanningTests(unittest.TestCase):
    def make_prd_pair(
        self,
        root: Path,
        name: str,
        *,
        completed: tuple[bool, ...],
    ) -> PlanningArtifacts:
        prd = root / "prd" / name / f"{name}.md"
        prd.parent.mkdir(parents=True)
        prd.write_text(f"# {name}\n", encoding="utf-8")
        issues = prd.parent / "issues" / "README.md"
        issues.parent.mkdir()
        links: list[str] = []
        for number, is_completed in enumerate(completed, start=1):
            issue = issues.parent / f"{number:04d}-issue.md"
            marker = "x" if is_completed else " "
            issue.write_text(
                f"# Issue {number:04d}\n\nCompleted: [{marker}]\n",
                encoding="utf-8",
            )
            links.append(f"- [Issue {number:04d}](./{issue.name})")
        issues.write_text("\n".join(links) + "\n", encoding="utf-8")
        return PlanningArtifacts(prd.resolve(), issues.resolve())

    def test_discovers_only_prd_packs_with_unfinished_issues(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            unfinished = self.make_prd_pair(
                root,
                "unfinished",
                completed=(True, False, False),
            )
            self.make_prd_pair(root, "finished", completed=(True, True))

            candidates = interactive_runner.find_resume_candidates(root)

        self.assertEqual([item.artifacts for item in candidates], [unfinished])
        self.assertEqual(candidates[0].completed_issues, 1)
        self.assertEqual(candidates[0].pending_issues, 2)
        self.assertEqual(candidates[0].total_issues, 3)

    def test_does_not_offer_an_explicit_codexcli_prd_to_portable_resume(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = self.make_prd_pair(root, "codexcli-only", completed=(False,))
            artifacts.prd_path.write_text(
                "# CodexCLI work\n\n"
                "## Target Product\n\n"
                "The separately installed `codexcli` Textual application.\n",
                encoding="utf-8",
            )

            candidates = interactive_runner.find_resume_candidates(root)

        self.assertEqual(candidates, [])

    def test_uses_loop_state_to_describe_the_active_issue(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = self.make_prd_pair(root, "active", completed=(True, False))
            state_path = artifacts.issues_index.with_name("README.loop.state.json")
            state_path.write_text(
                json.dumps(
                    {
                        "issues": {
                            "0001": {"status": "Completed"},
                            "0002": {"status": "In Progress: reviewer"},
                        }
                    }
                ),
                encoding="utf-8",
            )

            candidate = interactive_runner.find_resume_candidates(root)[0]

        self.assertEqual(candidate.active_issue, "0002")
        self.assertEqual(candidate.active_status, "In Progress: reviewer")

    def test_discovers_a_flat_local_issue_pack(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issues_root = root / "issues"
            issues_root.mkdir()
            prd = issues_root / "flat-feature.md"
            index = issues_root / "flat-feature-issues.md"
            issue = issues_root / "0001-flat-feature.md"
            prd.write_text("# Flat feature\n", encoding="utf-8")
            issue.write_text("# Issue\n\nCompleted: [ ]\n", encoding="utf-8")
            index.write_text(f"[Issue 0001](./{issue.name})\n", encoding="utf-8")

            candidates = interactive_runner.find_resume_candidates(root)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].artifacts.prd_path, prd.resolve())
        self.assertEqual(candidates[0].artifacts.issues_index, index.resolve())
        self.assertEqual(interactive_runner.artifact_slug(candidates[0].artifacts), "flat-feature")

    def test_startup_resume_returns_the_selected_unfinished_prd(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = self.make_prd_pair(root, "resume-me", completed=(False,))
            with mock.patch.object(
                interactive_runner,
                "ask_choice",
                side_effect=["2", "1"],
            ):
                selected = interactive_runner.choose_startup_artifacts(root)

        self.assertEqual(selected, artifacts)

    def test_startup_resume_skips_new_analysis_and_enters_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = self.make_prd_pair(root, "resume-me", completed=(False,))
            parser = interactive_runner.build_parser()
            args = parser.parse_args(["--repo", str(root)])
            bundle = mock.Mock(root=root)
            state_path = root / "devloop-plan.json"
            with mock.patch.object(
                interactive_runner.BundleContext,
                "from_file",
                return_value=bundle,
            ), mock.patch.object(
                interactive_runner,
                "plan_state_path",
                return_value=state_path,
            ), mock.patch.object(
                interactive_runner,
                "choose_target_repo",
                return_value=root,
            ), mock.patch.object(
                interactive_runner,
                "choose_startup_artifacts",
                return_value=artifacts,
            ), mock.patch.object(
                interactive_runner,
                "current_branch",
                return_value="main",
            ), mock.patch.object(
                interactive_runner,
                "print_prd_status",
            ), mock.patch.object(
                interactive_runner,
                "run_handoff",
                return_value=23,
            ) as run_handoff, mock.patch.object(
                interactive_runner,
                "apply_branch_strategy",
            ) as apply_branch_strategy, mock.patch.object(
                interactive_runner,
                "run_planning_chat",
            ) as run_planning_chat:
                result = interactive_runner._run_planning(parser, args)

        self.assertEqual(result, 23)
        apply_branch_strategy.assert_not_called()
        run_planning_chat.assert_not_called()
        run_handoff.assert_called_once()


class FindNewArtifactsTests(unittest.TestCase):
    def make_prd_pair(self, root: Path, name: str) -> None:
        prd = root / "prd" / name / f"{name}.md"
        prd.parent.mkdir(parents=True)
        prd.write_text("prd", encoding="utf-8")
        issues = root / "prd" / name / "issues" / "README.md"
        issues.parent.mkdir(parents=True)
        issues.write_text("issues", encoding="utf-8")

    def test_worktree_checkout_of_old_prd_is_not_detected(self) -> None:
        # Simulates `git worktree add` materializing a pre-existing PRD pair with
        # fresh (now) mtimes moments before started_at is captured. The snapshot is
        # taken first (mtimes are naturally >= started_at - slack), so the probe
        # must ignore it despite the fresh checkout mtimes.
        import time

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.make_prd_pair(root, "old-feature")
            baseline = interactive_runner.snapshot_artifacts(root)
            started_at = time.time()
            result = interactive_runner.find_new_artifacts(root, started_at, baseline)
        self.assertEqual(result, [])

    def test_new_pair_written_after_snapshot_is_detected(self) -> None:
        import time

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            baseline = interactive_runner.snapshot_artifacts(root)
            started_at = time.time() - 1
            self.make_prd_pair(root, "new-feature")
            result = interactive_runner.find_new_artifacts(root, started_at, baseline)
        self.assertEqual(len(result), 1)
        self.assertTrue(str(result[0].prd_path).endswith("new-feature.md"))

    def test_snapshotted_pair_retouched_forward_is_detected(self) -> None:
        import os
        import time

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.make_prd_pair(root, "edited-feature")
            baseline = interactive_runner.snapshot_artifacts(root)
            started_at = time.time()
            # Codex edits the snapshotted PRD after the chat begins: mtime advances.
            future = time.time() + 60
            prd = root / "prd" / "edited-feature" / "edited-feature.md"
            issues = root / "prd" / "edited-feature" / "issues" / "README.md"
            os.utime(prd, (future, future))
            os.utime(issues, (future, future))
            result = interactive_runner.find_new_artifacts(root, started_at, baseline)
        self.assertEqual(len(result), 1)
        self.assertTrue(str(result[0].prd_path).endswith("edited-feature.md"))

    def test_readme_fallback_index_is_ignored_by_probe(self) -> None:
        # A pair whose only index is prd/<name>/README.md (no issues/ dir) must be
        # ignored by the live probe, even though the --prd/manual paths accept it.
        import time

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            prd_folder = root / "prd" / "fallback"
            prd_folder.mkdir(parents=True)
            (prd_folder / "fallback.md").write_text("prd", encoding="utf-8")
            (prd_folder / "README.md").write_text("index", encoding="utf-8")
            started_at = time.time() - 1
            result = interactive_runner.find_new_artifacts(root, started_at, {})
        self.assertEqual(result, [])

    def test_preexisting_old_prd_is_not_detected(self) -> None:
        # An old pair (stale mtimes, absent from a fresh worktree snapshot) that is
        # not fresh enough is filtered out by the freshness slack alone.
        import os
        import time

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.make_prd_pair(root, "old-feature")
            past = time.time() - 3600
            for path in (root / "prd").rglob("*"):
                os.utime(path, (past, past))
            os.utime(root / "prd" / "old-feature", (past, past))
            started_at = time.time()
            result = interactive_runner.find_new_artifacts(root, started_at, {})
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
