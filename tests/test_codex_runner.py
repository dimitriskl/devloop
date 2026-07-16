from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from devloop import codex_runner
from devloop.codex_events import render_safe_codex_activity
from devloop.issue_pack import Issue
from devloop.portable_workflow import (
    FINAL_REVIEW_STEP_ID,
    SECURITY_REVIEW_STEP_ID,
    CodexExecutionSettings,
    ExecutionBudget,
    FastPreference,
)
from devloop.statusui import Stage
from devloop.templates import BundleContext, Preset
from tests.terminal_safety import (
    HOSTILE_TERMINAL_TEXT,
    assert_terminal_text_is_safe,
)


class ResolveCodexExecutableTests(unittest.TestCase):
    def test_uses_shutil_which_when_available(self) -> None:
        with mock.patch.object(
            codex_runner.shutil,
            "which",
            return_value="C:/Users/Dimitris/AppData/Roaming/npm/codex.cmd",
        ):
            result = codex_runner.resolve_codex_executable("codex")

        self.assertEqual(result, "C:/Users/Dimitris/AppData/Roaming/npm/codex.cmd")

    def test_falls_back_to_windows_npm_shim_location(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            appdata = Path(raw) / "Roaming"
            npm_dir = appdata / "npm"
            npm_dir.mkdir(parents=True)
            codex_cmd = npm_dir / "codex.cmd"
            codex_cmd.write_text("@echo off\n", encoding="utf-8")

            with mock.patch.object(codex_runner.shutil, "which", return_value=None), \
                 mock.patch.object(codex_runner.sys, "platform", "win32"), \
                 mock.patch.dict(os.environ, {"APPDATA": str(appdata)}):
                result = codex_runner.resolve_codex_executable("codex")

        self.assertEqual(result, str(codex_cmd.resolve()))


class CodexCommandSettingsTests(unittest.TestCase):
    def test_command_explicitly_overrides_model_effort_and_fast_on_or_off(self) -> None:
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(
            codex_runner,
            "uses_legacy_approval_flag",
            return_value=False,
        ):
            root = Path(raw)
            common = {
                "codex": "codex",
                "repo_root": root,
                "sandbox": "workspace-write",
                "approval_policy": "never",
                "schema_path": root / "schema.json",
                "message_path": root / "message.json",
            }
            for fast, tier, feature_switch in (
                (FastPreference.ON, 'service_tier="fast"', "--enable"),
                (FastPreference.OFF, 'service_tier="default"', "--disable"),
            ):
                with self.subTest(fast=fast):
                    command = codex_runner.build_codex_exec_command(
                        **common,
                        codex_settings=CodexExecutionSettings(
                            "gpt-5.6-sol",
                            "xhigh",
                            fast,
                        ),
                    )

                    self.assertIn("-m", command)
                    self.assertEqual(command[command.index("-m") + 1], "gpt-5.6-sol")
                    self.assertIn('model_reasoning_effort="xhigh"', command)
                    self.assertIn(tier, command)
                    self.assertIn(feature_switch, command)
                    self.assertEqual(
                        command[command.index(feature_switch) + 1],
                        "fast_mode",
                    )

    def test_streaming_command_enforces_the_step_execution_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result = codex_runner.run_streaming_codex_command(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                input_text="",
                cwd=Path(raw),
                stage=Stage.DEVELOPMENT,
                execution_budget=ExecutionBudget(
                    timeout_seconds=0.2,
                    checkpoint_seconds=0.2,
                ),
            )

        self.assertEqual(result.returncode, 124)
        self.assertIn("Execution Budget", result.stderr)

    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_streaming_budget_kills_child_retaining_inherited_pipes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            started_at = time.monotonic()
            result = codex_runner.run_streaming_codex_command(
                [
                    sys.executable,
                    "-c",
                    (
                        "import subprocess, sys; "
                        "subprocess.Popen([sys.executable, '-c', "
                        "'import time; time.sleep(5)'], stdout=sys.stdout, "
                        "stderr=sys.stderr)"
                    ),
                ],
                input_text="",
                cwd=Path(raw),
                stage=Stage.DEVELOPMENT,
                execution_budget=ExecutionBudget(
                    timeout_seconds=0.2,
                    checkpoint_seconds=0.2,
                ),
            )

        self.assertEqual(result.returncode, 124)
        self.assertLess(time.monotonic() - started_at, 2.0)

    def test_connection_retries_consume_one_attempt_execution_budget(self) -> None:
        runner = codex_runner.CodexRunner.__new__(codex_runner.CodexRunner)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runner.repo_root = root
            runner.log_root = root / ".loop.logs"
            runner.ensure_log_root()
            retryable = codex_runner.subprocess.CompletedProcess(
                ["codex"],
                1,
                stdout="",
                stderr="failed to connect to websocket\n",
            )
            successful = codex_runner.subprocess.CompletedProcess(
                ["codex"],
                0,
                stdout='{"status":"PASS"}\n',
                stderr="",
            )
            with mock.patch.object(
                codex_runner,
                "CODEX_CONNECTION_RETRY_DELAY_SECONDS",
                0.15,
            ), mock.patch.object(
                codex_runner,
                "run_streaming_codex_command",
                side_effect=(retryable, retryable, successful),
            ) as execute:
                result = runner.run_codex_exec_with_connection_retries(
                    command=["codex"],
                    prompt="Implement the issue.",
                    stdout_path=runner.log_root / "stdout.jsonl",
                    stderr_path=runner.log_root / "stderr.txt",
                    execution_budget=ExecutionBudget(
                        timeout_seconds=0.2,
                        checkpoint_seconds=0.2,
                    ),
                )

        self.assertEqual(execute.call_count, 2)
        self.assertEqual(result.returncode, 124)
        self.assertIn("Execution Budget", result.stderr)


class RolePromptIdentityTests(unittest.TestCase):
    def test_step_capabilities_and_guidance_override_role_defaults_in_the_prompt(
        self,
    ) -> None:
        repository_root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001.md"
            issue_path.write_text("# Security review\n", encoding="utf-8")
            runner = codex_runner.CodexRunner.__new__(codex_runner.CodexRunner)
            runner.bundle = BundleContext(
                root=repository_root,
                prompts=repository_root / "prompts",
                schemas=repository_root / "schemas",
            )
            runner.repo_root = root
            runner.prd_path = root / "prd.md"
            runner.issues_index = root / "README.md"
            runner.preset = Preset(
                name="test",
                required_docs=[],
                roles={
                    "reviewer": {
                        "skills": ["skills/codex/legacy/SKILL.md"],
                        "agents": ["agents/codex/legacy.md"],
                    }
                },
            )
            runner.use_self_improvement_wiki = False

            prompt = runner.build_prompt(
                role="reviewer",
                issue=Issue("0001", "Security review", issue_path, False),
                pass_number=1,
                fix_list=[],
                skill_paths=("skills/codex/security/SKILL.md",),
                agent_paths=(),
                step_guidance=(
                    "Focus on authentication boundaries.\n"
                    'password: "prompt-persistence-secret"\n'
                    '"password":\n  "prompt-next-line-secret"\n'
                    'password" : prompt-malformed-secret'
                ),
            )

        self.assertIn("skills/codex/security/SKILL.md", prompt)
        self.assertNotIn("skills/codex/legacy/SKILL.md", prompt)
        self.assertNotIn("agents/codex/legacy.md", prompt)
        self.assertIn("Focus on authentication boundaries.", prompt)
        self.assertIn("[redacted]", prompt)
        self.assertNotIn("prompt-persistence-secret", prompt)
        self.assertNotIn("prompt-next-line-secret", prompt)
        self.assertNotIn("prompt-malformed-secret", prompt)
        self.assertIn("permissions, and safety boundaries", prompt)

    def test_unterminated_private_key_is_redacted_from_the_role_prompt(self) -> None:
        repository_root = Path(__file__).parents[1]
        private_key_secret = "unterminated-prompt-private-key-secret"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001.md"
            issue_path.write_text("# Security review\n", encoding="utf-8")
            runner = codex_runner.CodexRunner.__new__(codex_runner.CodexRunner)
            runner.bundle = BundleContext(
                root=repository_root,
                prompts=repository_root / "prompts",
                schemas=repository_root / "schemas",
            )
            runner.repo_root = root
            runner.prd_path = root / "prd.md"
            runner.issues_index = root / "README.md"
            runner.preset = Preset(
                name="test",
                required_docs=[],
                roles={"reviewer": {"skills": [], "agents": []}},
            )
            runner.use_self_improvement_wiki = False

            prompt = runner.build_prompt(
                role="reviewer",
                issue=Issue("0001", "Security review", issue_path, False),
                pass_number=1,
                fix_list=[],
                step_guidance=(
                    "Inspect key handling.\n"
                    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
                    f"{private_key_secret}"
                ),
            )

        self.assertIn("Inspect key handling.", prompt)
        self.assertIn("[redacted-private-key]", prompt)
        self.assertNotIn(private_key_secret, prompt)

    def test_mismatched_private_key_end_is_redacted_from_persisted_role_prompt(
        self,
    ) -> None:
        repository_root = Path(__file__).parents[1]
        secret_fragments = (
            "mismatched-prompt-private-key-secret",
            "secret-after-mismatched-prompt-key-end",
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001.md"
            issue_path.write_text("# Security review\n", encoding="utf-8")
            runner = codex_runner.CodexRunner.__new__(codex_runner.CodexRunner)
            runner.bundle = BundleContext(
                root=repository_root,
                prompts=repository_root / "prompts",
                schemas=repository_root / "schemas",
            )
            runner.repo_root = root
            runner.prd_path = root / "prd.md"
            runner.issues_index = root / "README.md"
            runner.preset = Preset(
                name="test",
                required_docs=[],
                roles={"reviewer": {"skills": [], "agents": []}},
            )
            runner.codex = "codex"
            runner.sandbox = "workspace-write"
            runner.approval_policy = "never"
            runner.log_root = root / ".loop.logs"
            runner.use_self_improvement_wiki = False
            runner.ensure_log_root()

            with mock.patch.object(
                codex_runner,
                "build_codex_exec_command",
                return_value=["codex"],
            ), mock.patch.object(
                runner,
                "run_codex_exec_with_connection_retries",
                return_value=codex_runner.subprocess.CompletedProcess(
                    ["codex"],
                    0,
                    stdout='{"status":"PASS"}',
                    stderr="",
                ),
            ):
                runner.run_role(
                    role="reviewer",
                    issue=Issue("0001", "Security review", issue_path, False),
                    pass_number=1,
                    step_guidance=(
                        "Inspect key handling.\n"
                        "-----BEGIN RSA PRIVATE KEY-----\n"
                        f"{secret_fragments[0]}\n"
                        "-----END EC PRIVATE KEY-----\n"
                        f"{secret_fragments[1]}"
                    ),
                )

            persisted_prompt = next(
                runner.log_root.glob("*.prompt.md")
            ).read_text(encoding="utf-8")

        self.assertIn("Inspect key handling.", persisted_prompt)
        self.assertIn("[redacted-private-key]", persisted_prompt)
        for secret in secret_fragments:
            self.assertNotIn(secret, persisted_prompt)

    def test_concatenated_quoted_secret_is_redacted_from_persisted_role_prompt(
        self,
    ) -> None:
        repository_root = Path(__file__).parents[1]
        secret_fragments = (
            "concat-prompt-secret-alpha",
            "concat-prompt-secret-omega",
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001.md"
            issue_path.write_text("# Security review\n", encoding="utf-8")
            runner = codex_runner.CodexRunner.__new__(codex_runner.CodexRunner)
            runner.bundle = BundleContext(
                root=repository_root,
                prompts=repository_root / "prompts",
                schemas=repository_root / "schemas",
            )
            runner.repo_root = root
            runner.prd_path = root / "prd.md"
            runner.issues_index = root / "README.md"
            runner.preset = Preset(
                name="test",
                required_docs=[],
                roles={"reviewer": {"skills": [], "agents": []}},
            )
            runner.codex = "codex"
            runner.sandbox = "workspace-write"
            runner.approval_policy = "never"
            runner.log_root = root / ".loop.logs"
            runner.use_self_improvement_wiki = False
            runner.ensure_log_root()

            with mock.patch.object(
                codex_runner,
                "build_codex_exec_command",
                return_value=["codex"],
            ), mock.patch.object(
                runner,
                "run_codex_exec_with_connection_retries",
                return_value=codex_runner.subprocess.CompletedProcess(
                    ["codex"],
                    0,
                    stdout='{"status":"PASS"}',
                    stderr="",
                ),
            ):
                runner.run_role(
                    role="reviewer",
                    issue=Issue("0001", "Security review", issue_path, False),
                    pass_number=1,
                    step_guidance=(
                        "Inspect login handling.\n"
                        'password: "concat-prompt-secret-alpha" + '
                        '"concat-prompt-secret-omega"\n'
                        "Keep this safe instruction."
                    ),
                )

            persisted_prompt = next(
                runner.log_root.glob("*.prompt.md")
            ).read_text(encoding="utf-8")

        self.assertIn("Inspect login handling.", persisted_prompt)
        self.assertIn("Keep this safe instruction.", persisted_prompt)
        self.assertIn("[redacted]", persisted_prompt)
        for secret in secret_fragments:
            self.assertNotIn(secret, persisted_prompt)

    def test_placeholder_like_step_guidance_remains_literal_and_bounded(self) -> None:
        repository_root = Path(__file__).parents[1]
        literal_guidance = (
            "Treat {{FIX_LIST}} and {{REVIEW_RESULT}} as literal examples."
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001.md"
            issue_path.write_text("# Security review\n", encoding="utf-8")
            runner = codex_runner.CodexRunner.__new__(codex_runner.CodexRunner)
            runner.bundle = BundleContext(
                root=repository_root,
                prompts=repository_root / "prompts",
                schemas=repository_root / "schemas",
            )
            runner.repo_root = root
            runner.prd_path = root / "prd.md"
            runner.issues_index = root / "README.md"
            runner.preset = Preset(
                name="test",
                required_docs=[],
                roles={"reviewer": {"skills": [], "agents": []}},
            )
            runner.use_self_improvement_wiki = False

            prompt = runner.build_prompt(
                role="reviewer",
                issue=Issue("0001", "Security review", issue_path, False),
                pass_number=1,
                fix_list=["Do not substitute this finding."],
                review_result=codex_runner.RoleResult(
                    status="FAIL",
                    summary="Do not substitute this result.",
                ),
                step_guidance=literal_guidance,
            )

        guidance_section = prompt.split("## Step Guidance\n\n", 1)[1].split(
            "\n\n## Review Rules",
            1,
        )[0]
        self.assertIn(literal_guidance, guidance_section)
        self.assertNotIn("Do not substitute this finding.", guidance_section)
        self.assertNotIn("Do not substitute this result.", guidance_section)

    def test_custom_role_uses_its_adapter_prompt_and_installed_capabilities(
        self,
    ) -> None:
        repository_root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001.md"
            issue_path.write_text("# Security review\n", encoding="utf-8")
            runner = codex_runner.CodexRunner.__new__(codex_runner.CodexRunner)
            runner.bundle = BundleContext(
                root=repository_root,
                prompts=repository_root / "prompts",
                schemas=repository_root / "schemas",
            )
            runner.repo_root = root
            runner.prd_path = root / "prd.md"
            runner.issues_index = root / "README.md"
            runner.preset = Preset(
                name="test",
                required_docs=[],
                roles={
                    "security-review": {
                        "skills": ["skills/codex/senior-code-reviewer/SKILL.md"],
                        "agents": ["agents/codex/senior-code-reviewer.md"],
                    }
                },
            )
            runner.use_self_improvement_wiki = False

            prompt = runner.build_prompt(
                role="security-review",
                role_adapter="reviewer",
                issue=Issue("0001", "Security review", issue_path, False),
                pass_number=1,
                fix_list=[],
                step_display_name="Security Review",
            )

        self.assertIn("# Codex Dev Loop Senior Review", prompt)
        self.assertIn("skills/codex/senior-code-reviewer/SKILL.md", prompt)
        self.assertIn("agents/codex/senior-code-reviewer.md", prompt)
        self.assertIn("Workflow step: `Security Review`", prompt)

    def test_rendering_a_prompt_rejects_unsafe_markdown_display_names(self) -> None:
        repository_root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001.md"
            issue_path.write_text("# Security review\n", encoding="utf-8")
            runner = codex_runner.CodexRunner.__new__(codex_runner.CodexRunner)
            runner.bundle = BundleContext(
                root=repository_root,
                prompts=repository_root / "prompts",
                schemas=repository_root / "schemas",
            )
            runner.repo_root = root
            runner.prd_path = root / "prd.md"
            runner.issues_index = root / "README.md"
            runner.preset = Preset(
                name="test",
                required_docs=[],
                roles={"security-review": {"skills": [], "agents": []}},
            )
            runner.log_root = root / ".loop.logs"
            runner.use_self_improvement_wiki = False
            runner.ensure_log_root()

            with self.assertRaisesRegex(
                ValueError,
                "control characters or line breaks",
            ):
                runner.render_dry_run_prompts(
                    Issue("0001", "Security review", issue_path, False),
                    (
                        (
                            "security-review",
                            "reviewer",
                            "\x1b[2JInjected\nHeading",
                            str(SECURITY_REVIEW_STEP_ID),
                        ),
                    ),
                )

            self.assertEqual(list(runner.log_root.iterdir()), [])

    def test_duplicate_review_instances_get_distinct_prompt_sessions_and_logs(self) -> None:
        repository_root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001.md"
            issue_path.write_text("# Review identity\n", encoding="utf-8")
            runner = codex_runner.CodexRunner.__new__(codex_runner.CodexRunner)
            runner.bundle = BundleContext(
                root=repository_root,
                prompts=repository_root / "prompts",
                schemas=repository_root / "schemas",
            )
            runner.repo_root = root
            runner.prd_path = root / "prd.md"
            runner.issues_index = root / "README.md"
            runner.preset = Preset(
                name="test",
                required_docs=[],
                roles={"reviewer": {"skills": [], "agents": []}},
            )
            runner.codex = "codex"
            runner.sandbox = "workspace-write"
            runner.approval_policy = "never"
            runner.log_root = root / ".loop.logs"
            runner.use_self_improvement_wiki = False
            runner.ensure_log_root()
            issue = Issue("0001", "Review identity", issue_path, False)
            completed = codex_runner.subprocess.CompletedProcess(
                ["codex"],
                0,
                stdout='{"status":"PASS"}',
                stderr="",
            )

            with mock.patch.object(
                codex_runner,
                "build_codex_exec_command",
                return_value=["codex"],
            ), mock.patch.object(
                runner,
                "run_codex_exec_with_connection_retries",
                return_value=completed,
            ):
                runner.run_role(
                    role="reviewer",
                    issue=issue,
                    pass_number=1,
                    step_instance_id=str(SECURITY_REVIEW_STEP_ID),
                    step_display_name="Security Review",
                    prompt_session_id="security-session",
                )
                runner.run_role(
                    role="reviewer",
                    issue=issue,
                    pass_number=1,
                    step_instance_id=str(FINAL_REVIEW_STEP_ID),
                    step_display_name="Final Review",
                    prompt_session_id="final-session",
                )

            prompts = sorted(runner.log_root.glob("*.prompt.md"))
            self.assertEqual(len(prompts), 2)
            self.assertNotEqual(prompts[0].name, prompts[1].name)
            prompt_text = "\n".join(
                path.read_text(encoding="utf-8") for path in prompts
            )
            self.assertIn("Workflow step: `Security Review`", prompt_text)
            self.assertIn("Prompt session: `security-session`", prompt_text)
            self.assertIn("Workflow step: `Final Review`", prompt_text)
            self.assertIn("Prompt session: `final-session`", prompt_text)

    def test_live_attempt_artifacts_remain_distinct_for_repeated_step_attempts(self) -> None:
        repository_root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001.md"
            issue_path.write_text("# Repeated review attempt\n", encoding="utf-8")
            runner = codex_runner.CodexRunner.__new__(codex_runner.CodexRunner)
            runner.bundle = BundleContext(
                root=repository_root,
                prompts=repository_root / "prompts",
                schemas=repository_root / "schemas",
            )
            runner.repo_root = root
            runner.prd_path = root / "prd.md"
            runner.issues_index = root / "README.md"
            runner.preset = Preset(
                name="test",
                required_docs=[],
                roles={"reviewer": {"skills": [], "agents": []}},
            )
            runner.codex = "codex"
            runner.sandbox = "workspace-write"
            runner.approval_policy = "never"
            runner.log_root = root / ".loop.logs"
            runner.use_self_improvement_wiki = False
            runner.ensure_log_root()
            issue = Issue("0001", "Repeated review attempt", issue_path, False)
            attempts = iter(("first", "second"))

            def build_command(**arguments: object) -> list[str]:
                return ["codex", "-o", str(arguments["message_path"])]

            def execute(command: list[str], **_: object) -> codex_runner.subprocess.CompletedProcess[str]:
                marker = next(attempts)
                message_path = Path(command[command.index("-o") + 1])
                message_path.write_text(
                    json.dumps({"status": "PASS", "summary": marker}),
                    encoding="utf-8",
                )
                return codex_runner.subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=f"stdout-{marker}\n",
                    stderr=f"stderr-{marker}\n",
                )

            with mock.patch.object(
                codex_runner,
                "build_codex_exec_command",
                side_effect=build_command,
            ), mock.patch.object(
                runner,
                "run_codex_exec_with_connection_retries",
                side_effect=execute,
            ):
                for attempt_id, session_id in (
                    ("attempt/one", "session-one"),
                    ("attempt/two", "session-two"),
                ):
                    runner.run_role(
                        role="reviewer",
                        issue=issue,
                        pass_number=1,
                        step_instance_id=str(SECURITY_REVIEW_STEP_ID),
                        step_display_name="Security Review",
                        step_attempt_id=attempt_id,
                        prompt_session_id=session_id,
                    )

            artifact_paths = {
                "prompt": sorted(runner.log_root.glob("*.prompt.md")),
                "stdout": sorted(runner.log_root.glob("*.stdout.jsonl")),
                "stderr": sorted(runner.log_root.glob("*.stderr.txt")),
                "last-message": sorted(runner.log_root.glob("*.last-message.json")),
            }
            for artifact_type, paths in artifact_paths.items():
                with self.subTest(artifact_type=artifact_type):
                    self.assertEqual(len(paths), 2)
                    self.assertNotEqual(paths[0].name, paths[1].name)
                    self.assertTrue(
                        any("attempt-attempt-one" in path.name for path in paths)
                    )
                    self.assertTrue(
                        any("attempt-attempt-two" in path.name for path in paths)
                    )
            stdout_contents = {
                path.read_text(encoding="utf-8")
                for path in artifact_paths["stdout"]
            }
            stderr_contents = {
                path.read_text(encoding="utf-8")
                for path in artifact_paths["stderr"]
            }
            last_message_summaries = {
                json.loads(path.read_text(encoding="utf-8"))["summary"]
                for path in artifact_paths["last-message"]
            }
            prompt_text = "\n".join(
                path.read_text(encoding="utf-8") for path in artifact_paths["prompt"]
            )

            self.assertEqual(
                stdout_contents,
                {"stdout-first\n", "stdout-second\n"},
            )
            self.assertEqual(
                stderr_contents,
                {"stderr-first\n", "stderr-second\n"},
            )
            self.assertEqual(last_message_summaries, {"first", "second"})
            self.assertIn("Prompt session: `session-one`", prompt_text)
            self.assertIn("Prompt session: `session-two`", prompt_text)

    def test_custom_role_path_separators_are_sanitized_in_log_filenames(self) -> None:
        repository_root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001.md"
            issue_path.write_text("# Security review\n", encoding="utf-8")
            runner = codex_runner.CodexRunner.__new__(codex_runner.CodexRunner)
            runner.bundle = BundleContext(
                root=repository_root,
                prompts=repository_root / "prompts",
                schemas=repository_root / "schemas",
            )
            runner.repo_root = root
            runner.prd_path = root / "prd.md"
            runner.issues_index = root / "README.md"
            runner.preset = Preset(
                name="test",
                required_docs=[],
                roles={"security/review": {"skills": [], "agents": []}},
            )
            runner.codex = "codex"
            runner.sandbox = "workspace-write"
            runner.approval_policy = "never"
            runner.log_root = root / ".loop.logs"
            runner.use_self_improvement_wiki = False
            runner.ensure_log_root()
            completed = codex_runner.subprocess.CompletedProcess(
                ["codex"],
                0,
                stdout='{"status":"PASS"}',
                stderr="",
            )

            with mock.patch.object(
                codex_runner,
                "build_codex_exec_command",
                return_value=["codex"],
            ), mock.patch.object(
                runner,
                "run_codex_exec_with_connection_retries",
                return_value=completed,
            ):
                result = runner.run_role(
                    role="security/review",
                    role_adapter="reviewer",
                    issue=Issue("0001", "Security review", issue_path, False),
                    pass_number=1,
                    step_instance_id=str(SECURITY_REVIEW_STEP_ID),
                    step_display_name="Security Review",
                )

            prompt_paths = list(runner.log_root.glob("*.prompt.md"))

        self.assertEqual(result.status, "PASS")
        self.assertEqual(len(prompt_paths), 1)
        self.assertIn("security-review", prompt_paths[0].name)

    def test_dry_run_filenames_use_instance_ids_to_avoid_slug_collisions(self) -> None:
        repository_root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001.md"
            issue_path.write_text("# Review collision\n", encoding="utf-8")
            runner = codex_runner.CodexRunner.__new__(codex_runner.CodexRunner)
            runner.bundle = BundleContext(
                root=repository_root,
                prompts=repository_root / "prompts",
                schemas=repository_root / "schemas",
            )
            runner.repo_root = root
            runner.prd_path = root / "prd.md"
            runner.issues_index = root / "README.md"
            runner.preset = Preset(
                name="test",
                required_docs=[],
                roles={"security/review": {"skills": [], "agents": []}},
            )
            runner.log_root = root / ".loop.logs"
            runner.use_self_improvement_wiki = False
            runner.ensure_log_root()

            runner.render_dry_run_prompts(
                Issue("0001", "Review collision", issue_path, False),
                (
                    (
                        "security/review",
                        "reviewer",
                        "A+B",
                        str(SECURITY_REVIEW_STEP_ID),
                    ),
                    (
                        "security/review",
                        "reviewer",
                        "A B",
                        str(FINAL_REVIEW_STEP_ID),
                    ),
                ),
            )
            prompt_names = sorted(
                path.name for path in runner.log_root.glob("*.prompt.md")
            )

        self.assertEqual(len(prompt_names), 2)
        self.assertTrue(all("security-review" in name for name in prompt_names))
        self.assertTrue(
            any(str(SECURITY_REVIEW_STEP_ID) in name for name in prompt_names)
        )
        self.assertTrue(any(str(FINAL_REVIEW_STEP_ID) in name for name in prompt_names))

    def test_log_writes_are_confined_to_the_configured_log_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runner = codex_runner.CodexRunner.__new__(codex_runner.CodexRunner)
            runner.log_root = root / ".loop.logs"
            runner.ensure_log_root()
            escaped_path = runner.log_root / ".." / "escaped.prompt.md"

            with self.assertRaisesRegex(ValueError, "outside the configured log root"):
                runner.write_log_text(escaped_path, "unsafe")

            self.assertFalse((root / "escaped.prompt.md").exists())

    def test_run_role_writes_the_allowlisted_triggering_attempt_record_to_coder_prompt(
        self,
    ) -> None:
        repository_root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001.md"
            issue_path.write_text("# Rework prompt\n", encoding="utf-8")
            runner = codex_runner.CodexRunner.__new__(codex_runner.CodexRunner)
            runner.bundle = BundleContext(
                root=repository_root,
                prompts=repository_root / "prompts",
                schemas=repository_root / "schemas",
            )
            runner.repo_root = root
            runner.prd_path = root / "prd.md"
            runner.issues_index = root / "README.md"
            runner.preset = Preset(
                name="test",
                required_docs=[],
                roles={"coder": {"skills": [], "agents": []}},
            )
            runner.codex = "codex"
            runner.sandbox = "workspace-write"
            runner.approval_policy = "never"
            runner.log_root = root / ".loop.logs"
            runner.use_self_improvement_wiki = False
            runner.ensure_log_root()
            triggering_record = {
                "attempt_id": "review-attempt-17",
                "step_instance_id": str(SECURITY_REVIEW_STEP_ID),
                "issue_id": "0001",
                "pass": 1,
                "prompt_session_id": "security-review-session",
                "outcome": "CHANGES_REQUESTED",
                "result": {
                    "status": "FAIL",
                    "summary": "Security review requested a correction.",
                    "changed_files": [],
                    "verification_commands": [],
                    "findings": ["SEC-017 reaches the shell."],
                    "fix_list": ["Correct SEC-017."],
                    "residual_risks": [],
                },
            }

            completed = codex_runner.subprocess.CompletedProcess(
                ["codex"],
                0,
                stdout='{"status":"PASS"}',
                stderr="",
            )

            with mock.patch.object(
                codex_runner,
                "build_codex_exec_command",
                return_value=["codex"],
            ), mock.patch.object(
                runner,
                "run_codex_exec_with_connection_retries",
                return_value=completed,
            ):
                runner.run_role(
                    role="coder",
                    issue=Issue("0001", "Rework prompt", issue_path, False),
                    pass_number=2,
                    fix_list=["Correct SEC-017."],
                    rework_attempt_record=triggering_record,
                )
            prompt = next(runner.log_root.glob("*.prompt.md")).read_text(
                encoding="utf-8"
            )

        self.assertIn("## Triggering Rework Step Attempt Record", prompt)
        self.assertIn('"attempt_id": "review-attempt-17"', prompt)
        self.assertIn(f'"step_instance_id": "{SECURITY_REVIEW_STEP_ID}"', prompt)
        self.assertIn('"findings": [', prompt)
        self.assertNotIn("{{REWORK_ATTEMPT_RECORD}}", prompt)


class StreamingCodexRunnerTests(unittest.TestCase):
    def test_every_delivery_role_maps_to_its_visible_phase(self) -> None:
        self.assertIs(codex_runner.stage_for_role("coder"), Stage.DEVELOPMENT)
        self.assertIs(codex_runner.stage_for_role("reviewer"), Stage.REVIEW)
        self.assertIs(codex_runner.stage_for_role("qa"), Stage.QA)

    def test_unknown_delivery_role_fails_clearly(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported Dev Loop role"):
            codex_runner.stage_for_role("mystery")

    def test_reasoning_activity_never_exposes_raw_chain_of_thought(self) -> None:
        activity = render_safe_codex_activity(
            {
                "type": "item.started",
                "item": {
                    "type": "reasoning",
                    "text": "private reasoning must not be displayed",
                },
            }
        )

        self.assertEqual(activity, "Codex is reasoning about the task.")
        self.assertNotIn("private reasoning", activity)

    def test_agent_update_strips_terminal_control_characters(self) -> None:
        activity = render_safe_codex_activity(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": HOSTILE_TERMINAL_TEXT,
                },
            }
        )

        self.assertIsNotNone(activity)
        assert_terminal_text_is_safe(
            self,
            activity or "",
            redirected=True,
        )

    def test_role_execution_streams_safe_activity_before_process_exit(self) -> None:
        class OpenAfterCompletion:
            def __init__(self) -> None:
                self._lines = iter(
                    [
                        (
                            '{"type":"item.completed","item":'
                            '{"type":"agent_message",'
                            '"text":"Inspecting the acceptance criteria."}}\n'
                        ),
                        (
                            '{"type":"item.started","item":'
                            '{"type":"command_execution","command":"secret command",'
                            '"status":"in_progress"}}\n'
                        ),
                        '{"type":"turn.completed","usage":{}}\n',
                    ]
                )

            def __iter__(self):
                return self

            def __next__(self) -> str:
                try:
                    return next(self._lines)
                except StopIteration as error:
                    raise AssertionError(
                        "runner read past turn.completed and would wait for pipe EOF"
                    ) from error

        class FakeProcess:
            def __init__(self) -> None:
                self.stdin = StringIO()
                self.stdout = OpenAfterCompletion()
                self.stderr: list[str] = []
                self.returncode: int | None = None

            def wait(self, timeout=None):
                self.returncode = 0
                return self.returncode

            def terminate(self) -> None:
                self.returncode = -15

            def kill(self) -> None:
                self.returncode = -9

        runner = codex_runner.CodexRunner.__new__(codex_runner.CodexRunner)
        runner.repo_root = Path("/tmp/repository")
        with mock.patch.object(
                 codex_runner.subprocess,
                 "Popen",
                 return_value=FakeProcess(),
             ) as popen, redirect_stdout(StringIO()) as stdout:
            result = runner.run_codex_exec_with_connection_retries(
                command=["codex", "exec", "--json", "-"],
                prompt="Implement the issue.",
                stdout_path=Path("stdout.jsonl"),
                stderr_path=Path("stderr.txt"),
            )

        self.assertEqual(result.returncode, 0)
        popen.assert_called_once()
        rendered = stdout.getvalue()
        self.assertIn(
            "[development] Codex update: Inspecting the acceptance criteria.",
            rendered,
        )
        self.assertIn("[development] Running a repository command.", rendered)
        self.assertNotIn("secret command", rendered)

    def test_live_dashboard_callback_receives_activity_without_printing_log_lines(self) -> None:
        class FakeProcess:
            def __init__(self) -> None:
                self.stdin = StringIO()
                self.stdout = iter(
                    [
                        (
                            '{"type":"item.completed","item":'
                            '{"type":"agent_message",'
                            '"text":"Checking the catalog."}}\n'
                        ),
                        '{"type":"turn.completed","usage":{}}\n',
                    ]
                )
                self.stderr: list[str] = []
                self.returncode: int | None = None

            def wait(self, timeout=None):
                self.returncode = 0
                return self.returncode

            def terminate(self) -> None:
                self.returncode = -15

            def kill(self) -> None:
                self.returncode = -9

        activities: list[str | None] = []
        with mock.patch.object(
            codex_runner.subprocess,
            "Popen",
            return_value=FakeProcess(),
        ), redirect_stdout(StringIO()) as stdout:
            result = codex_runner.run_streaming_codex_command(
                ["codex", "exec", "--json", "-"],
                input_text="Implement the issue.",
                cwd=Path("/tmp/repository"),
                stage=Stage.REVIEW,
                activity_callback=activities.append,
            )

        self.assertEqual(result.returncode, 0)
        self.assertIn("Codex update: Checking the catalog.", activities)
        self.assertEqual(stdout.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
