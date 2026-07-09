from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from devloop import github_install


def fake_git_runner(populate):
    def runner(command, *, cwd=None, input_text=None):
        if command[0] == "git" and command[1] == "clone":
            clone_dir = Path(command[-1])
            clone_dir.mkdir(parents=True, exist_ok=True)
            populate(clone_dir)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="unexpected")

    return runner


def repo_with_skill_and_agent(clone_dir: Path) -> None:
    skill = clone_dir / "skills" / "my-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("skill", encoding="utf-8")
    (skill / "extra.md").write_text("extra", encoding="utf-8")
    agents = clone_dir / "agents"
    agents.mkdir(parents=True)
    (agents / "my-agent.md").write_text("agent", encoding="utf-8")


class FindCandidatesTests(unittest.TestCase):
    def test_skill_folder_named_agents_is_not_double_classified(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            skill_dir = root / "skills" / "agents"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("skill", encoding="utf-8")
            agents_dir = root / "agents"
            agents_dir.mkdir(parents=True)
            (agents_dir / "real-agent.md").write_text("agent", encoding="utf-8")

            candidates = github_install.find_candidates(root)
            labels = sorted(f"{c.kind}:{c.name}" for c in candidates)
            self.assertEqual(labels, ["agent:real-agent", "skill:agents"])


class DefaultCloneRunnerTests(unittest.TestCase):
    def test_install_default_runner_is_env_guarded(self) -> None:
        import inspect

        signature = inspect.signature(github_install.install_from_github)
        self.assertIs(
            signature.parameters["runner"].default,
            github_install._default_clone_runner,
        )

    def test_default_clone_runner_sets_prompt_guard_env(self) -> None:
        captured: dict = {}

        def fake_run(command, *, cwd=None, input_text=None, env=None):
            captured["command"] = list(command)
            captured["env"] = env
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with mock.patch("devloop.github_install.run_captured_text", fake_run):
            github_install._default_clone_runner(["git", "clone", "url", "dest"])

        self.assertEqual(captured["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(captured["env"]["GCM_INTERACTIVE"], "never")


class ParseRefTests(unittest.TestCase):
    def test_plain_url(self) -> None:
        self.assertEqual(
            github_install.parse_github_ref("https://github.com/o/r"),
            ("https://github.com/o/r", ""),
        )

    def test_url_with_subpath(self) -> None:
        self.assertEqual(
            github_install.parse_github_ref("https://github.com/o/r#skills/my-skill"),
            ("https://github.com/o/r", "skills/my-skill"),
        )


class InstallTests(unittest.TestCase):
    def test_installs_skill_and_agent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = Path(raw)
            result = github_install.install_from_github(
                "https://github.com/o/r",
                bundle,
                runner=fake_git_runner(repo_with_skill_and_agent),
                confirm=lambda message: True,
            )
            self.assertEqual(sorted(result.installed), ["agent:my-agent", "skill:my-skill"])
            self.assertTrue((bundle / "skills" / "codex" / "my-skill" / "SKILL.md").is_file())
            self.assertTrue((bundle / "skills" / "codex" / "my-skill" / "extra.md").is_file())
            self.assertTrue((bundle / "agents" / "codex" / "my-agent.md").is_file())
            self.assertFalse((bundle / ".install-tmp").exists())

    def test_existing_skill_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = Path(raw)
            existing = bundle / "skills" / "codex" / "my-skill"
            existing.mkdir(parents=True)
            (existing / "SKILL.md").write_text("old", encoding="utf-8")
            result = github_install.install_from_github(
                "https://github.com/o/r",
                bundle,
                runner=fake_git_runner(repo_with_skill_and_agent),
                confirm=lambda message: True,
            )
            self.assertEqual(result.installed, ["agent:my-agent"])
            self.assertIn("already exists", result.message)
            self.assertEqual(
                (existing / "SKILL.md").read_text(encoding="utf-8"), "old"
            )

    def test_declined_confirmation_installs_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = Path(raw)
            result = github_install.install_from_github(
                "https://github.com/o/r",
                bundle,
                runner=fake_git_runner(repo_with_skill_and_agent),
                confirm=lambda message: False,
            )
            self.assertEqual(result.installed, [])
            self.assertFalse((bundle / "skills" / "codex" / "my-skill").exists())

    def test_no_candidates_reports_message(self) -> None:
        def populate(clone_dir: Path) -> None:
            (clone_dir / "README.md").write_text("nothing", encoding="utf-8")

        with tempfile.TemporaryDirectory() as raw:
            result = github_install.install_from_github(
                "https://github.com/o/r",
                Path(raw),
                runner=fake_git_runner(populate),
                confirm=lambda message: True,
            )
            self.assertEqual(result.installed, [])
            self.assertIn("No skills or agents found", result.message)

    def test_clone_failure_reports_message(self) -> None:
        def runner(command, *, cwd=None, input_text=None):
            return subprocess.CompletedProcess(command, 128, stdout="", stderr="fatal: not found")

        with tempfile.TemporaryDirectory() as raw:
            result = github_install.install_from_github(
                "https://github.com/o/missing",
                Path(raw),
                runner=runner,
                confirm=lambda message: True,
            )
            self.assertEqual(result.installed, [])
            self.assertIn("git clone failed", result.message)

    def test_subpath_limits_search(self) -> None:
        def populate(clone_dir: Path) -> None:
            repo_with_skill_and_agent(clone_dir)
            other = clone_dir / "other" / "other-skill"
            other.mkdir(parents=True)
            (other / "SKILL.md").write_text("other", encoding="utf-8")

        with tempfile.TemporaryDirectory() as raw:
            bundle = Path(raw)
            result = github_install.install_from_github(
                "https://github.com/o/r#skills",
                bundle,
                runner=fake_git_runner(populate),
                confirm=lambda message: True,
            )
            self.assertEqual(result.installed, ["skill:my-skill"])

    def test_copy_failure_is_contained_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = Path(raw)
            with mock.patch(
                "devloop.github_install.shutil.copy2", side_effect=OSError("disk full")
            ):
                result = github_install.install_from_github(
                    "https://github.com/o/r",
                    bundle,
                    runner=fake_git_runner(repo_with_skill_and_agent),
                    confirm=lambda message: True,
                )
            self.assertEqual(result.installed, ["skill:my-skill"])
            self.assertIn("Failed:", result.message)
            self.assertIn("disk full", result.message)

    def test_subpath_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = Path(raw)
            result = github_install.install_from_github(
                "https://github.com/o/r#../outside",
                bundle,
                runner=fake_git_runner(repo_with_skill_and_agent),
                confirm=lambda message: True,
            )
            self.assertEqual(result.installed, [])
            self.assertIn("Invalid subpath", result.message)


if __name__ == "__main__":
    unittest.main()
