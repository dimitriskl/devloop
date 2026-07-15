from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from devloop import chat_loop
from devloop.chat_loop import ChatCallbacks, ChatConfig, ChatSession


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


class BuildTurnCommandTests(unittest.TestCase):
    def make_session(self) -> ChatSession:
        config = ChatConfig(
            codex="codex",
            repo_root=Path("C:/repo"),
            bundle_root=Path("F:/devloop"),
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
        self.assertIn("Codex is working [|]", rendered)
        self.assertIn("Codex is working [/]", rendered)

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
        return ChatConfig(codex="codex", repo_root=repo, bundle_root=repo / "bundle")

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
