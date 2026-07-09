from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
