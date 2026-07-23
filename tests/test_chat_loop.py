from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest import mock

from devloop import chat_loop
from devloop.chat_loop import ChatCallbacks, ChatConfig, ChatSession
from devloop.portable_workflow import (
    ANALYSIS_STEP_ID,
    CodexExecutionSettings,
    ExecutionBudget,
    FastPreference,
    StepRuntimeState,
    StepRuntimeStatus,
    default_codex_execution_settings,
    default_portable_component_catalog,
    default_portable_workflow,
)
from devloop.statusui import (
    DashboardStatus,
    IssueResultSummary,
    project_workflow_progress,
)
from tests.terminal_safety import (
    HOSTILE_TERMINAL_TEXT,
    assert_terminal_text_is_safe,
)


class ParseSessionIdTests(unittest.TestCase):
    def test_finds_uuid_on_session_line(self) -> None:
        output = "workdir: /x\nsession id: 0198c0de-1111-2222-3333-444455556666\nmodel: gpt-5\n"
        self.assertEqual(
            chat_loop.parse_session_id(output),
            "0198c0de-1111-2222-3333-444455556666",
        )

    def test_ignores_uuid_on_unrelated_line(self) -> None:
        output = "request id 0198c0de-1111-2222-3333-444455556666\n"
        self.assertIsNone(chat_loop.parse_session_id(output))

    def test_finds_thread_id_from_json_event(self) -> None:
        output = (
            '{"type":"thread.started",'
            '"thread_id":"0198c0de-1111-2222-3333-444455556666"}\n'
        )
        self.assertEqual(
            chat_loop.parse_session_id(output),
            "0198c0de-1111-2222-3333-444455556666",
        )

    def test_none_when_absent(self) -> None:
        self.assertIsNone(chat_loop.parse_session_id("no ids here"))


class DetectImagePathsTests(unittest.TestCase):
    def test_detects_existing_image_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            image = Path(raw) / "shot.png"
            image.write_bytes(b"png")
            found = chat_loop.detect_image_paths(f"see {image} please")
        self.assertEqual(found, [image.resolve()])

    def test_ignores_missing_files_and_non_images(self) -> None:
        self.assertEqual(chat_loop.detect_image_paths("no /tmp/missing.png here"), [])
        self.assertEqual(chat_loop.detect_image_paths("read docs/readme.md now"), [])


class AnalysisExecutionBudgetTests(unittest.TestCase):
    def test_streaming_analysis_enforces_its_snapshotted_budget(self) -> None:
        with tempfile.TemporaryDirectory() as raw, redirect_stdout(StringIO()):
            returncode, output = chat_loop.run_streaming(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                Path(raw),
                execution_budget=ExecutionBudget(0.2, 0.2),
            )

        self.assertEqual(returncode, 124)
        self.assertIn("Execution Budget", output)

    def test_streaming_analysis_keeps_an_active_backend_command_alive(self) -> None:
        script = (
            "import json, time; "
            "print(json.dumps({'type':'item.started','item':"
            "{'id':'command-1','type':'command_execution','status':'in_progress'}}), "
            "flush=True); "
            "time.sleep(0.5); "
            "print(json.dumps({'type':'item.completed','item':"
            "{'id':'command-1','type':'command_execution','status':'completed'}}), "
            "flush=True); "
            "print(json.dumps({'type':'turn.completed','usage':{}}), flush=True)"
        )
        with tempfile.TemporaryDirectory() as raw, redirect_stdout(StringIO()):
            returncode, output = chat_loop.run_streaming(
                [sys.executable, "-c", script, "--json"],
                Path(raw),
                execution_budget=ExecutionBudget(2, 0.1),
            )

        self.assertEqual(returncode, 0, output)
        self.assertNotIn("checkpoint deadline", output)


class ResumeCommandTests(unittest.TestCase):
    def test_resume_at_first_prompt_does_not_start_codex(self) -> None:
        turns: list[list[str]] = []

        def turn_runner(command, cwd):
            turns.append(list(command))
            return 0, "unexpected"

        callbacks = ChatCallbacks(
            probe_artifacts=lambda: None,
            manual_artifacts=lambda: None,
            open_options=lambda: None,
            status_summary=lambda: "status",
            resume_artifacts=lambda: "RESUMED-ARTIFACTS",
        )
        with tempfile.TemporaryDirectory() as raw:
            result = chat_loop.run_planning_chat(
                config=ChatConfig(
                    codex="codex",
                    repo_root=Path(raw),
                    bundle_root=Path(raw),
                    codex_settings=default_codex_execution_settings("analysis"),
                ),
                initial_prompt="PLAN",
                callbacks=callbacks,
                collect_initial_message=True,
                turn_runner=turn_runner,
                editor=FakeEditor(["/resume"]),
            )

        self.assertEqual(result, "RESUMED-ARTIFACTS")
        self.assertEqual(turns, [])

    def test_resume_returns_selected_artifacts_without_sending_command_to_codex(self) -> None:
        turns: list[list[str]] = []

        def turn_runner(command, cwd):
            turns.append(list(command))
            return 0, "session id: 0198c0de-1111-2222-3333-444455556666\n"

        callbacks = ChatCallbacks(
            probe_artifacts=lambda: None,
            manual_artifacts=lambda: None,
            open_options=lambda: None,
            status_summary=lambda: "status",
            resume_artifacts=lambda: "RESUMED-ARTIFACTS",
        )
        with tempfile.TemporaryDirectory() as raw:
            result = chat_loop.run_planning_chat(
                config=ChatConfig(
                    codex="codex",
                    repo_root=Path(raw),
                    bundle_root=Path(raw),
                    codex_settings=default_codex_execution_settings("analysis"),
                ),
                initial_prompt="PLAN",
                callbacks=callbacks,
                turn_runner=turn_runner,
                editor=FakeEditor(["/resume"]),
            )

        self.assertEqual(result, "RESUMED-ARTIFACTS")
        self.assertEqual(len(turns), 1)

    def test_help_lists_resume(self) -> None:
        self.assertIn("/resume", chat_loop.HELP_TEXT)

    def test_help_describes_options_as_the_workflow_editor(self) -> None:
        callbacks = ChatCallbacks(
            probe_artifacts=lambda: None,
            manual_artifacts=lambda: None,
            open_options=lambda: None,
            status_summary=lambda: "status",
        )
        with tempfile.TemporaryDirectory() as raw, redirect_stdout(
            StringIO()
        ) as output:
            result = chat_loop.run_planning_chat(
                config=ChatConfig(
                    codex="codex",
                    repo_root=Path(raw),
                    bundle_root=Path(raw),
                    codex_settings=default_codex_execution_settings("analysis"),
                ),
                initial_prompt="PLAN",
                callbacks=callbacks,
                collect_initial_message=True,
                editor=FakeEditor(["/help", "/quit"]),
            )

        self.assertIsNone(result)
        self.assertIn("/options open the Workflow Editor", output.getvalue())
        self.assertNotIn("development options", output.getvalue())


class BuildTurnCommandTests(unittest.TestCase):
    def make_session(self) -> ChatSession:
        config = ChatConfig(
            codex="codex",
            repo_root=Path("C:/repo"),
            bundle_root=Path("F:/devloop"),
            codex_settings=default_codex_execution_settings("analysis"),
        )
        return ChatSession(config=config)

    def test_first_turn_uses_plain_exec_with_prompt(self) -> None:
        session = self.make_session()
        command = session.build_turn_command("", first_prompt="PLAN PROMPT")
        self.assertEqual(command[:2], ["codex", "exec"])
        self.assertIn("--json", command)
        self.assertNotIn("resume", command)
        self.assertEqual(command[-1], "PLAN PROMPT")
        self.assertIn("--add-dir", command)
        self.assertIn("-s", command)

    def test_resume_turn_uses_session_id(self) -> None:
        session = self.make_session()
        session.session_id = "0198c0de-1111-2222-3333-444455556666"
        command = session.build_turn_command("next message")
        self.assertEqual(command[:4], ["codex", "exec", "resume", session.session_id])
        self.assertIn("--json", command)
        self.assertEqual(command[-1], "next message")

    def test_resume_without_id_falls_back_to_last(self) -> None:
        session = self.make_session()
        command = session.build_turn_command("next message")
        self.assertEqual(command[:4], ["codex", "exec", "resume", "--last"])
        self.assertIn("--json", command)

    def test_pending_images_added_as_image_flags(self) -> None:
        session = self.make_session()
        session.session_id = "0198c0de-1111-2222-3333-444455556666"
        session.pending_images = [Path("C:/tmp/a.png"), Path("C:/tmp/b.png")]
        command = session.build_turn_command("with images")
        self.assertEqual(command.count("-i"), 2)
        self.assertIn("C:/tmp/a.png", [part.replace("\\", "/") for part in command])

    def test_fresh_and_resumed_analysis_turns_keep_exact_snapshotted_settings(self) -> None:
        session = ChatSession(
            config=ChatConfig(
                codex="codex",
                repo_root=Path("C:/repo"),
                bundle_root=Path("F:/devloop"),
                codex_settings=CodexExecutionSettings(
                    "gpt-5.6-sol",
                    "xhigh",
                    FastPreference.OFF,
                ),
            )
        )

        first = session.build_turn_command("", first_prompt="PLAN")
        resumed = session.build_turn_command("CONTINUE")

        for command in (first, resumed):
            self.assertIn("-m", command)
            self.assertEqual(command[command.index("-m") + 1], "gpt-5.6-sol")
            self.assertIn('model_reasoning_effort="xhigh"', command)
            self.assertIn('service_tier="default"', command)
            self.assertIn("--disable", command)
            self.assertEqual(command[command.index("--disable") + 1], "fast_mode")

    # --- Added beyond the brief: lock down the real `codex exec resume --help`
    # contract (Codex CLI 0.143.0). Verified live: resume does NOT accept
    # -C/--cd, --add-dir, or -s/--sandbox (only exec's top level does); it DOES
    # accept -c, --skip-git-repo-check, and -i. See task-6-report.md Step 1.

    def test_resume_turn_omits_unsupported_exec_only_options(self) -> None:
        session = self.make_session()
        session.session_id = "0198c0de-1111-2222-3333-444455556666"
        command = session.build_turn_command("next message")
        self.assertNotIn("-C", command)
        self.assertNotIn("--add-dir", command)
        self.assertNotIn("-s", command)

    def test_resume_turn_keeps_options_supported_by_resume(self) -> None:
        session = self.make_session()
        session.session_id = "0198c0de-1111-2222-3333-444455556666"
        command = session.build_turn_command("next message")
        self.assertIn("-c", command)
        self.assertIn("--skip-git-repo-check", command)

    def test_first_turn_still_includes_full_option_set(self) -> None:
        session = self.make_session()
        command = session.build_turn_command("", first_prompt="PLAN PROMPT")
        self.assertIn("-C", command)
        self.assertIn("-c", command)
        self.assertIn("--skip-git-repo-check", command)


class RunStreamingTests(unittest.TestCase):
    class FakeProcess:
        stdout = ["ok\n"]
        returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def terminate(self):
            pass

        def kill(self):
            pass

    def test_missing_executable_returns_127_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            returncode, output = chat_loop.run_streaming(
                ["definitely-not-a-real-binary-xyz"], Path(raw)
            )
        self.assertEqual(returncode, 127)
        self.assertIn("not found", output)

    def test_waiting_duration_keeps_hours_visible(self) -> None:
        self.assertEqual(chat_loop._format_duration(7384.9), "02:03:04")

    def test_resolves_executable_before_starting_process(self) -> None:
        with tempfile.TemporaryDirectory() as raw, \
             mock.patch.object(
                 chat_loop,
                 "resolve_codex_executable",
                 return_value="C:/Users/Dimitris/AppData/Roaming/npm/codex.cmd",
             ), \
             mock.patch.object(
                 chat_loop.subprocess,
                 "Popen",
                 return_value=self.FakeProcess(),
             ) as popen:
            returncode, output = chat_loop.run_streaming(["codex", "--version"], Path(raw))

        self.assertEqual(returncode, 0)
        self.assertEqual(output, "ok\n")
        command = popen.call_args.args[0]
        self.assertEqual(command[0], "C:/Users/Dimitris/AppData/Roaming/npm/codex.cmd")
        self.assertEqual(command[1], "--version")

    def test_json_mode_suppresses_codex_prompt_echo(self) -> None:
        process = self.FakeProcess()
        process.stdout = [
            "Reading additional input from stdin...\n",
            '{"type":"thread.started",'
            '"thread_id":"0198c0de-1111-2222-3333-444455556666"}\n',
            (
                '{"type":"item.completed","item":{"type":"message",'
                '"role":"assistant","content":[{"type":"output_text",'
                '"text":"Starting grill-with-docs now."}]}}\n'
            ),
        ]
        with tempfile.TemporaryDirectory() as raw, \
             mock.patch.object(
                 chat_loop,
                 "resolve_codex_executable",
                 return_value="C:/Users/Dimitris/AppData/Roaming/npm/codex.cmd",
             ), \
             mock.patch.object(
                 chat_loop.subprocess,
                 "Popen",
                 return_value=process,
             ), \
             redirect_stdout(StringIO()) as stdout:
            returncode, output = chat_loop.run_streaming(
                ["codex", "exec", "--json", "PROMPT TEXT"], Path(raw)
            )

        self.assertEqual(returncode, 0)
        self.assertIn('"type":"thread.started"', output)
        rendered = stdout.getvalue()
        self.assertIn("Starting grill-with-docs now.", rendered)
        self.assertNotIn("Reading additional input from stdin", rendered)
        self.assertNotIn("PROMPT TEXT", rendered)

    def test_json_mode_renders_current_agent_message_text(self) -> None:
        process = self.FakeProcess()
        process.stdout = [
            (
                '{"type":"item.completed","item":{"id":"item_3",'
                '"type":"agent_message","text":"Planning response."}}\n'
            ),
        ]
        with tempfile.TemporaryDirectory() as raw, \
             mock.patch.object(
                 chat_loop,
                 "resolve_codex_executable",
                 return_value="C:/Users/Dimitris/AppData/Roaming/npm/codex.cmd",
             ), \
             mock.patch.object(
                 chat_loop.subprocess,
                 "Popen",
                 return_value=process,
             ), \
             redirect_stdout(StringIO()) as stdout:
            returncode, _ = chat_loop.run_streaming(
                ["codex", "exec", "--json", "PROMPT TEXT"], Path(raw)
            )

        self.assertEqual(returncode, 0)
        self.assertEqual(stdout.getvalue(), "Planning response.\n")

    def test_agent_messages_cannot_inject_terminal_controls(self) -> None:
        class OutputStream(io.StringIO):
            def __init__(self, *, tty: bool) -> None:
                super().__init__()
                self._tty = tty

            @property
            def encoding(self) -> str:
                return "utf-8"

            def isatty(self) -> bool:
                return self._tty

        event = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": HOSTILE_TERMINAL_TEXT,
                },
            },
            ensure_ascii=False,
        )
        for tty in (True, False):
            for json_mode in (True, False):
                with self.subTest(
                    tty=tty,
                    json_mode=json_mode,
                ), tempfile.TemporaryDirectory() as raw:
                    process = self.FakeProcess()
                    process.stdout = [
                        f"{event if json_mode else HOSTILE_TERMINAL_TEXT}\n"
                    ]
                    output = OutputStream(tty=tty)
                    command = ["codex", "exec"]
                    if json_mode:
                        command.append("--json")
                    command.append("PROMPT TEXT")
                    with mock.patch.object(
                        chat_loop,
                        "resolve_codex_executable",
                        return_value="codex",
                    ), mock.patch.object(
                        chat_loop.subprocess,
                        "Popen",
                        return_value=process,
                    ), mock.patch.object(
                        chat_loop,
                        "WaitingIndicator",
                        return_value=mock.Mock(),
                    ), redirect_stdout(output):
                        returncode, _ = chat_loop.run_streaming(
                            command,
                            Path(raw),
                        )

                    self.assertEqual(returncode, 0)
                    assert_terminal_text_is_safe(
                        self,
                        output.getvalue(),
                        redirected=not tty,
                    )

    def test_turn_completed_stops_reading_and_reaps_lingering_process(self) -> None:
        class OpenAfterCompletion:
            def __init__(self) -> None:
                self._lines = iter(
                    [
                        (
                            '{"type":"item.completed","item":'
                            '{"type":"agent_message","text":"Plan complete."}}\n'
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
                        "Devloop read past turn.completed and would wait for pipe EOF"
                    ) from error

        class LingeringProcess:
            def __init__(self) -> None:
                self.stdout = OpenAfterCompletion()
                self.returncode = None
                self.terminated = False

            def wait(self, timeout=None):
                if not self.terminated:
                    if timeout is None:
                        raise AssertionError("Devloop used an unbounded process wait")
                    raise subprocess.TimeoutExpired(["codex"], timeout)
                self.returncode = -15
                return self.returncode

            def terminate(self) -> None:
                self.terminated = True

            def kill(self) -> None:
                self.terminated = True

        process = LingeringProcess()
        with tempfile.TemporaryDirectory() as raw, \
             mock.patch.object(
                 chat_loop,
                 "resolve_codex_executable",
                 return_value="codex",
             ), \
             mock.patch.object(
                 chat_loop.subprocess,
                 "Popen",
                 return_value=process,
             ), \
             redirect_stdout(StringIO()) as stdout:
            returncode, output = chat_loop.run_streaming(
                ["codex", "exec", "--json", "PROMPT TEXT"], Path(raw)
            )

        self.assertEqual(returncode, 0)
        self.assertTrue(process.terminated)
        self.assertIn('"type":"turn.completed"', output)
        self.assertEqual(stdout.getvalue(), "Plan complete.\n")

    def test_turn_failed_is_terminal_and_returns_failure(self) -> None:
        process = self.FakeProcess()
        process.stdout = [
            '{"type":"turn.failed","error":{"message":"Planning failed."}}\n'
        ]
        with tempfile.TemporaryDirectory() as raw, \
             mock.patch.object(
                 chat_loop,
                 "resolve_codex_executable",
                 return_value="codex",
             ), \
             mock.patch.object(
                 chat_loop.subprocess,
                 "Popen",
                 return_value=process,
             ), \
             redirect_stdout(StringIO()) as stdout:
            returncode, output = chat_loop.run_streaming(
                ["codex", "exec", "--json", "PROMPT TEXT"], Path(raw)
            )

        self.assertEqual(returncode, 1)
        self.assertIn('"type":"turn.failed"', output)
        self.assertEqual(stdout.getvalue(), "ERROR: Planning failed.\n")

    def test_waiting_indicator_runs_between_visible_output(self) -> None:
        process = self.FakeProcess()
        process.stdout = [
            (
                '{"type":"item.completed","item":{"type":"agent_message",'
                '"text":"Planning response."}}\n'
            ),
        ]
        indicator = mock.Mock()
        with tempfile.TemporaryDirectory() as raw, \
             mock.patch.object(
                 chat_loop,
                 "resolve_codex_executable",
                 return_value="C:/Users/Dimitris/AppData/Roaming/npm/codex.cmd",
             ), \
             mock.patch.object(
                 chat_loop.subprocess,
                 "Popen",
                 return_value=process,
             ), \
             mock.patch.object(
                 chat_loop,
                 "WaitingIndicator",
                 return_value=indicator,
                 create=True,
             ), \
             redirect_stdout(StringIO()):
            chat_loop.run_streaming(
                ["codex", "exec", "--json", "PROMPT TEXT"], Path(raw)
            )

        self.assertEqual(
            indicator.method_calls,
            [
                mock.call.start(),
                mock.call.notify_activity(),
                mock.call.stop(),
                mock.call.start(),
                mock.call.stop(),
            ],
        )

    def test_streaming_analysis_uses_the_shared_progress_projection(self) -> None:
        process = self.FakeProcess()
        process.stdout = [
            (
                '{"type":"item.completed","item":{"type":"agent_message",'
                '"text":"Planning response."}}\n'
            ),
        ]
        workflow = default_portable_workflow()
        progress = project_workflow_progress(
            workflow,
            default_portable_component_catalog(),
            (
                StepRuntimeState(
                    step_instance_id=ANALYSIS_STEP_ID,
                    issue_id=None,
                    status=StepRuntimeStatus.RUNNING,
                    pass_number=1,
                ),
            ),
            (),
            issue_id=None,
        )
        indicator = mock.Mock()

        with tempfile.TemporaryDirectory() as raw, mock.patch.object(
            chat_loop,
            "resolve_codex_executable",
            return_value="codex",
        ), mock.patch.object(
            chat_loop.subprocess,
            "Popen",
            return_value=process,
        ), mock.patch.object(
            chat_loop,
            "WaitingIndicator",
            return_value=indicator,
        ) as indicator_type, redirect_stdout(StringIO()):
            chat_loop.run_streaming(
                ["codex", "exec", "--json", "PROMPT TEXT"],
                Path(raw),
                workflow_progress=progress,
            )

        self.assertIs(
            indicator_type.call_args.kwargs["workflow_progress"],
            progress,
        )
        self.assertIn(
            mock.call.notify_activity("Planning response."),
            indicator.method_calls,
        )

    def test_waiting_indicator_animates_multiple_frames(self) -> None:
        class InteractiveOutput(StringIO):
            def isatty(self) -> bool:
                return True

        class StopAfterTwoFrames:
            def __init__(self) -> None:
                self.wait_count = 0

            def wait(self, timeout: float) -> bool:
                self.wait_count += 1
                return self.wait_count >= 2

        output = InteractiveOutput()
        indicator = chat_loop.WaitingIndicator(output)
        indicator._stop_requested = StopAfterTwoFrames()

        indicator._animate()

        rendered = output.getvalue()
        self.assertIn("Codex is WORKING [|]", rendered)
        self.assertIn("Codex is WORKING [/]", rendered)

    def test_waiting_indicator_reports_stage_elapsed_activity_and_stall(self) -> None:
        class InteractiveOutput(StringIO):
            def isatty(self) -> bool:
                return True

        class Clock:
            value = 0.0

            def __call__(self) -> float:
                return self.value

        clock = Clock()
        indicator = chat_loop.WaitingIndicator(
            InteractiveOutput(),
            clock=clock,
            stalled_after_seconds=120.0,
        )

        clock.value = 65.0
        starting = indicator._status_line("|")
        indicator.notify_activity()
        clock.value = 80.0
        active = indicator._status_line("/")
        clock.value = 200.0
        stalled = indicator._status_line("-")

        self.assertIn("[analysis]", starting)
        self.assertIn("elapsed 00:01:05", starting)
        self.assertIn("waiting for first event", starting)
        self.assertIn("last event 00:00:15 ago", active)
        self.assertIn("POSSIBLY STALLED", stalled)
        self.assertIn("silent 00:02:15", stalled)
        self.assertIn("Ctrl+C", stalled)
        self.assertLessEqual(len(stalled), 79)


class FakeEditor:
    def __init__(self, lines: list[str]) -> None:
        self.lines = list(lines)

    def read_line(self, prompt: str) -> str:
        if not self.lines:
            raise EOFError
        return self.lines.pop(0)


class RunPlanningChatTests(unittest.TestCase):
    def make_config(self, repo: Path) -> ChatConfig:
        return ChatConfig(
            codex="codex",
            repo_root=repo,
            bundle_root=repo / "bundle",
            codex_settings=default_codex_execution_settings("analysis"),
        )

    def test_planning_surface_renders_the_shared_workflow_projection(self) -> None:
        workflow = default_portable_workflow()
        progress = project_workflow_progress(
            workflow,
            default_portable_component_catalog(),
            (
                StepRuntimeState(
                    step_instance_id=ANALYSIS_STEP_ID,
                    issue_id=None,
                    status=StepRuntimeStatus.RUNNING,
                    pass_number=1,
                ),
            ),
            (),
            issue_id=None,
            activity="Planning the PRD and issue pack.",
        )
        callbacks = ChatCallbacks(
            probe_artifacts=lambda: "ARTIFACTS",
            manual_artifacts=lambda: None,
            open_options=lambda: None,
            status_summary=lambda: "status",
        )

        with tempfile.TemporaryDirectory() as raw, redirect_stdout(StringIO()) as output:
            config = self.make_config(Path(raw))
            config.workflow_progress = progress
            result = chat_loop.run_planning_chat(
                config=config,
                initial_prompt="PLAN",
                callbacks=callbacks,
                turn_runner=lambda *_: (0, "ok"),
                editor=FakeEditor([]),
            )

        self.assertEqual(result, "ARTIFACTS")
        self.assertIn("WORKFLOW", output.getvalue())
        self.assertIn("ISSUE STEPS", output.getvalue())
        self.assertIn(
            "ACTIVE Analysis · model gpt-5.6-sol · effort xhigh · Fast OFF",
            output.getvalue(),
        )

    def test_planning_surface_sanitizes_every_dynamic_progress_field(self) -> None:
        progress = project_workflow_progress(
            default_portable_workflow(),
            default_portable_component_catalog(),
            (
                StepRuntimeState(
                    step_instance_id=ANALYSIS_STEP_ID,
                    issue_id=None,
                    status=StepRuntimeStatus.RUNNING,
                    pass_number=1,
                ),
            ),
            (),
            issue_id=None,
        )
        active = replace(
            progress.workflow_steps[0],
            display_name=f"{HOSTILE_TERMINAL_TEXT} {'x' * 500}",
            model=HOSTILE_TERMINAL_TEXT,
            reasoning_effort=HOSTILE_TERMINAL_TEXT,
            fast=HOSTILE_TERMINAL_TEXT,
        )
        issue_step = replace(
            progress.issue_steps[0],
            display_name=HOSTILE_TERMINAL_TEXT,
        )
        progress = replace(
            progress,
            workflow_steps=(active,),
            issue_steps=(issue_step, *progress.issue_steps[1:]),
            activity=replace(progress.activity, safe_text=HOSTILE_TERMINAL_TEXT),
            issue_title=HOSTILE_TERMINAL_TEXT,
            issue_history=(
                IssueResultSummary(
                    issue_number=HOSTILE_TERMINAL_TEXT,
                    status=DashboardStatus.PASS,
                    pass_number=1,
                    elapsed_seconds=1,
                ),
            ),
        )
        callbacks = ChatCallbacks(
            probe_artifacts=lambda: "ARTIFACTS",
            manual_artifacts=lambda: None,
            open_options=lambda: None,
            status_summary=lambda: "status",
        )

        with (
            tempfile.TemporaryDirectory() as raw,
            redirect_stdout(StringIO()) as output,
            mock.patch(
                "devloop.statusui.shutil.get_terminal_size",
                return_value=os.terminal_size((1200, 40)),
            ),
        ):
            config = self.make_config(Path(raw))
            config.workflow_progress = progress
            chat_loop.run_planning_chat(
                config=config,
                initial_prompt="PLAN",
                callbacks=callbacks,
                turn_runner=lambda *_: (0, "ok"),
                editor=FakeEditor([]),
            )

        rendered = output.getvalue()
        assert_terminal_text_is_safe(self, rendered, redirected=True)
        self.assertNotIn("x" * 500, rendered)
        self.assertIn("model Καλημέρα 世界 ESC-CSI C1-CSI BIDI", rendered)
        self.assertIn("effort Καλημέρα 世界 ESC-CSI C1-CSI BIDI", rendered)
        self.assertIn("Fast Καλημέρα 世界 E...", rendered)

    def test_returns_artifacts_when_probe_finds_them(self) -> None:
        turns: list[list[str]] = []
        artifacts_box = {"ready": False}

        def turn_runner(command, cwd):
            turns.append(list(command))
            artifacts_box["ready"] = True
            return 0, "session id: 0198c0de-1111-2222-3333-444455556666\nok\n"

        callbacks = ChatCallbacks(
            probe_artifacts=lambda: "ARTIFACTS" if artifacts_box["ready"] else None,
            manual_artifacts=lambda: None,
            open_options=lambda: None,
            status_summary=lambda: "status",
        )
        with tempfile.TemporaryDirectory() as raw:
            result = chat_loop.run_planning_chat(
                config=self.make_config(Path(raw)),
                initial_prompt="PLAN",
                callbacks=callbacks,
                turn_runner=turn_runner,
                editor=FakeEditor([]),
            )
        self.assertEqual(result, "ARTIFACTS")
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0][-1], "PLAN")

    def test_prints_submit_confirmation_before_codex_turn(self) -> None:
        printed = StringIO()
        turn_started = {"value": False}

        def turn_runner(command, cwd):
            turn_started["value"] = True
            self.assertIn("Submitted to Codex", printed.getvalue())
            self.assertIn("ANALYSIS active", printed.getvalue())
            self.assertIn("PRD + ISSUES not detected", printed.getvalue())
            self.assertIn("DEVELOPMENT waits", printed.getvalue())
            return 0, "session id: 0198c0de-1111-2222-3333-444455556666\nok\n"

        callbacks = ChatCallbacks(
            probe_artifacts=lambda: "ARTIFACTS" if turn_started["value"] else None,
            manual_artifacts=lambda: None,
            open_options=lambda: None,
            status_summary=lambda: "status",
        )
        with tempfile.TemporaryDirectory() as raw, redirect_stdout(printed):
            result = chat_loop.run_planning_chat(
                config=self.make_config(Path(raw)),
                initial_prompt="PLAN",
                callbacks=callbacks,
                turn_runner=turn_runner,
                editor=FakeEditor([]),
            )
        self.assertEqual(result, "ARTIFACTS")

    def test_collects_initial_message_before_first_codex_turn(self) -> None:
        turns: list[list[str]] = []
        artifacts_box = {"ready": False}

        def turn_runner(command, cwd):
            turns.append(list(command))
            artifacts_box["ready"] = True
            return 0, "session id: 0198c0de-1111-2222-3333-444455556666\nok\n"

        callbacks = ChatCallbacks(
            probe_artifacts=lambda: "ARTIFACTS" if artifacts_box["ready"] else None,
            manual_artifacts=lambda: None,
            open_options=lambda: None,
            status_summary=lambda: "status",
        )
        with tempfile.TemporaryDirectory() as raw:
            result = chat_loop.run_planning_chat(
                config=self.make_config(Path(raw)),
                initial_prompt="PLAN",
                callbacks=callbacks,
                collect_initial_message=True,
                turn_runner=turn_runner,
                editor=FakeEditor(["build a login page"]),
            )
        self.assertEqual(result, "ARTIFACTS")
        self.assertEqual(len(turns), 1)
        self.assertNotIn("resume", turns[0])
        self.assertIn("PLAN", turns[0][-1])
        self.assertIn("Initial user goal:\nbuild a login page", turns[0][-1])

    def test_collect_initial_message_can_quit_before_codex_turn(self) -> None:
        turns: list[list[str]] = []

        def turn_runner(command, cwd):
            turns.append(list(command))
            return 0, "session id: 0198c0de-1111-2222-3333-444455556666\nok\n"

        callbacks = ChatCallbacks(
            probe_artifacts=lambda: None,
            manual_artifacts=lambda: None,
            open_options=lambda: None,
            status_summary=lambda: "status",
        )
        with tempfile.TemporaryDirectory() as raw:
            result = chat_loop.run_planning_chat(
                config=self.make_config(Path(raw)),
                initial_prompt="PLAN",
                callbacks=callbacks,
                collect_initial_message=True,
                turn_runner=turn_runner,
                editor=FakeEditor(["/quit", "y"]),
            )
        self.assertIsNone(result)
        self.assertEqual(turns, [])

    def test_user_message_sent_as_resume_turn(self) -> None:
        turns: list[list[str]] = []
        state = {"count": 0}

        def turn_runner(command, cwd):
            turns.append(list(command))
            state["count"] += 1
            return 0, "session id: 0198c0de-1111-2222-3333-444455556666\nok\n"

        callbacks = ChatCallbacks(
            probe_artifacts=lambda: "DONE" if state["count"] >= 2 else None,
            manual_artifacts=lambda: None,
            open_options=lambda: None,
            status_summary=lambda: "status",
        )
        with tempfile.TemporaryDirectory() as raw:
            result = chat_loop.run_planning_chat(
                config=self.make_config(Path(raw)),
                initial_prompt="PLAN",
                callbacks=callbacks,
                turn_runner=turn_runner,
                editor=FakeEditor(["build a login page"]),
            )
        self.assertEqual(result, "DONE")
        self.assertEqual(turns[1][2], "resume")
        self.assertEqual(turns[1][-1], "build a login page")

    def test_quit_returns_none(self) -> None:
        def turn_runner(command, cwd):
            return 0, "session id: 0198c0de-1111-2222-3333-444455556666\n"

        callbacks = ChatCallbacks(
            probe_artifacts=lambda: None,
            manual_artifacts=lambda: None,
            open_options=lambda: None,
            status_summary=lambda: "status",
        )
        with tempfile.TemporaryDirectory() as raw:
            result = chat_loop.run_planning_chat(
                config=self.make_config(Path(raw)),
                initial_prompt="PLAN",
                callbacks=callbacks,
                turn_runner=turn_runner,
                editor=FakeEditor(["/quit", "y"]),
            )
        self.assertIsNone(result)

    def test_failed_turn_keeps_loop_alive(self) -> None:
        state = {"count": 0}

        def turn_runner(command, cwd):
            state["count"] += 1
            if state["count"] == 2:
                return 1, "boom"
            return 0, "session id: 0198c0de-1111-2222-3333-444455556666\n"

        callbacks = ChatCallbacks(
            probe_artifacts=lambda: "OK" if state["count"] >= 3 else None,
            manual_artifacts=lambda: None,
            open_options=lambda: None,
            status_summary=lambda: "status",
        )
        with tempfile.TemporaryDirectory() as raw:
            result = chat_loop.run_planning_chat(
                config=self.make_config(Path(raw)),
                initial_prompt="PLAN",
                callbacks=callbacks,
                turn_runner=turn_runner,
                editor=FakeEditor(["first try", "second try"]),
            )
        self.assertEqual(result, "OK")

    def test_first_turn_failure_retries_initial_before_message(self) -> None:
        turns: list[list[str]] = []
        state = {"count": 0}

        def turn_runner(command, cwd):
            turns.append(list(command))
            state["count"] += 1
            if state["count"] == 1:
                return 1, "boom"
            return 0, "session id: 0198c0de-1111-2222-3333-444455556666\nok\n"

        callbacks = ChatCallbacks(
            probe_artifacts=lambda: "DONE" if state["count"] >= 3 else None,
            manual_artifacts=lambda: None,
            open_options=lambda: None,
            status_summary=lambda: "status",
        )
        with tempfile.TemporaryDirectory() as raw:
            result = chat_loop.run_planning_chat(
                config=self.make_config(Path(raw)),
                initial_prompt="PLAN",
                callbacks=callbacks,
                turn_runner=turn_runner,
                editor=FakeEditor(["build a login page"]),
            )
        self.assertEqual(result, "DONE")
        self.assertEqual(len(turns), 3)
        # turn 1: failed initial exec; turn 2: retried initial exec (plain, not resume)
        self.assertNotIn("resume", turns[1])
        self.assertEqual(turns[1][-1], "PLAN")
        # turn 3: the user's message as a resume turn
        self.assertEqual(turns[2][2], "resume")
        self.assertEqual(turns[2][-1], "build a login page")

    def test_image_temp_dir_removed_on_exit(self) -> None:
        created: list[Path] = []
        real_mkdtemp = chat_loop.tempfile.mkdtemp

        def tracking_mkdtemp(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            created.append(Path(path))
            return path

        def turn_runner(command, cwd):
            return 0, "session id: 0198c0de-1111-2222-3333-444455556666\n"

        callbacks = ChatCallbacks(
            probe_artifacts=lambda: "DONE",
            manual_artifacts=lambda: None,
            open_options=lambda: None,
            status_summary=lambda: "status",
        )
        from unittest import mock
        with tempfile.TemporaryDirectory() as raw, \
             mock.patch.object(chat_loop.tempfile, "mkdtemp", tracking_mkdtemp):
            chat_loop.run_planning_chat(
                config=self.make_config(Path(raw)),
                initial_prompt="PLAN",
                callbacks=callbacks,
                turn_runner=turn_runner,
                editor=FakeEditor([]),
            )
        image_dirs = [p for p in created if p.name.startswith("devloop-images-")]
        self.assertEqual(len(image_dirs), 1)
        self.assertFalse(image_dirs[0].exists())

    def test_done_command_uses_manual_fallback(self) -> None:
        def turn_runner(command, cwd):
            return 0, "session id: 0198c0de-1111-2222-3333-444455556666\n"

        callbacks = ChatCallbacks(
            probe_artifacts=lambda: None,
            manual_artifacts=lambda: "MANUAL",
            open_options=lambda: None,
            status_summary=lambda: "status",
        )
        with tempfile.TemporaryDirectory() as raw:
            result = chat_loop.run_planning_chat(
                config=self.make_config(Path(raw)),
                initial_prompt="PLAN",
                callbacks=callbacks,
                turn_runner=turn_runner,
                editor=FakeEditor(["/done"]),
            )
        self.assertEqual(result, "MANUAL")

    def test_failed_resume_falls_back_to_fresh_exec(self) -> None:
        turns: list[list[str]] = []
        state = {"count": 0}
        planning_contract = (
            "PLAN\nDo not start implementation.\nCreate the PRD and issue pack."
        )

        def turn_runner(command, cwd):
            turns.append(list(command))
            state["count"] += 1
            # turn 1: initial planning exec (ok, first session id)
            # turn 2: resume for the first user message (fails)
            # turn 3: fresh exec fallback (ok, NEW session id)
            # turn 4: resume of the NEW session (ok) -> probe then succeeds
            if state["count"] == 2:
                return 1, "resume failed"
            if state["count"] == 3:
                return 0, "session id: 0198aaaa-1111-2222-3333-444455556666\nok\n"
            return 0, "session id: 0198c0de-1111-2222-3333-444455556666\nok\n"

        callbacks = ChatCallbacks(
            probe_artifacts=lambda: "DONE" if state["count"] >= 4 else None,
            manual_artifacts=lambda: None,
            open_options=lambda: None,
            status_summary=lambda: "status",
        )
        with tempfile.TemporaryDirectory() as raw:
            result = chat_loop.run_planning_chat(
                config=self.make_config(Path(raw)),
                initial_prompt=planning_contract,
                callbacks=callbacks,
                turn_runner=turn_runner,
                editor=FakeEditor(["first message", "second message", "third message"]),
            )
        self.assertEqual(result, "DONE")
        # turn 3 (index 2): fresh exec fallback, not a resume; prompt carries the
        # original planning contract, continuation note, and user's message text.
        self.assertNotIn("resume", turns[2])
        self.assertIn(planning_contract, turns[2][-1])
        self.assertIn("Continuing an interrupted Dev Loop planning session", turns[2][-1])
        self.assertIn("second message", turns[2][-1])
        # turn 4 (index 3): resumes the NEW session id captured from turn 3.
        self.assertEqual(turns[3][2], "resume")
        self.assertEqual(turns[3][3], "0198aaaa-1111-2222-3333-444455556666")

    def test_keyboard_interrupt_during_turn_keeps_loop_alive(self) -> None:
        state = {"count": 0}

        def turn_runner(command, cwd):
            state["count"] += 1
            if state["count"] == 2:
                raise KeyboardInterrupt
            return 0, "session id: 0198c0de-1111-2222-3333-444455556666\nok\n"

        callbacks = ChatCallbacks(
            probe_artifacts=lambda: "DONE" if state["count"] >= 3 else None,
            manual_artifacts=lambda: None,
            open_options=lambda: None,
            status_summary=lambda: "status",
        )
        with tempfile.TemporaryDirectory() as raw:
            result = chat_loop.run_planning_chat(
                config=self.make_config(Path(raw)),
                initial_prompt="PLAN",
                callbacks=callbacks,
                turn_runner=turn_runner,
                editor=FakeEditor(["first", "second"]),
            )
        # The interrupt on turn 2 does not escape; the loop consumes "second"
        # (turn 3) and finishes normally.
        self.assertEqual(result, "DONE")
        self.assertGreaterEqual(state["count"], 3)

    def test_unknown_slash_command_is_sent_as_turn(self) -> None:
        turns: list[list[str]] = []
        state = {"count": 0}

        def turn_runner(command, cwd):
            turns.append(list(command))
            state["count"] += 1
            return 0, "session id: 0198c0de-1111-2222-3333-444455556666\nok\n"

        callbacks = ChatCallbacks(
            probe_artifacts=lambda: "DONE" if state["count"] >= 2 else None,
            manual_artifacts=lambda: None,
            open_options=lambda: None,
            status_summary=lambda: "status",
        )
        with tempfile.TemporaryDirectory() as raw:
            result = chat_loop.run_planning_chat(
                config=self.make_config(Path(raw)),
                initial_prompt="PLAN",
                callbacks=callbacks,
                turn_runner=turn_runner,
                editor=FakeEditor(["/unknown stuff"]),
            )
        self.assertEqual(result, "DONE")
        # turn 1 = initial exec; turn 2 = the "/unknown stuff" line sent verbatim.
        self.assertEqual(turns[1][-1], "/unknown stuff")

    def test_status_reports_no_artifacts_line(self) -> None:
        import io
        from contextlib import redirect_stdout

        def turn_runner(command, cwd):
            return 0, "session id: 0198c0de-1111-2222-3333-444455556666\n"

        callbacks = ChatCallbacks(
            probe_artifacts=lambda: None,
            manual_artifacts=lambda: None,
            open_options=lambda: None,
            status_summary=lambda: "status",
        )
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as raw, redirect_stdout(buf):
            chat_loop.run_planning_chat(
                config=self.make_config(Path(raw)),
                initial_prompt="PLAN",
                callbacks=callbacks,
                turn_runner=turn_runner,
                editor=FakeEditor(["/status"]),
            )
        self.assertIn("Artifacts: none detected yet", buf.getvalue())
        self.assertIn("ANALYSIS active", buf.getvalue())
        self.assertIn("PRD + ISSUES not detected", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
