from __future__ import annotations

import io
import os
import select
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from devloop.cli_ui import render_screen_frame
from devloop.terminal_editor import IteratorKeySource
from devloop.portable_runtime import portable_plain_mode_session
from devloop.terminal_menu import (
    KeyEvent,
    MenuAction,
    NavigationKey,
    choose_menu_option,
    clear_terminal_screen,
    read_navigation_key,
    read_workflow_command,
    render_app_screen,
)


class TerminalMenuTests(unittest.TestCase):
    def test_plain_mode_never_opens_the_legacy_full_screen_menu(self) -> None:
        rendered: list[str] = []
        with portable_plain_mode_session(), mock.patch(
            "devloop.terminal_menu._open_navigation_source",
            side_effect=AssertionError("plain mode must not open a raw key source"),
        ):
            choice = choose_menu_option(
                (("1", "Start"), ("2", "Resume")),
                default_key="1",
                render=rendered.append,
                fallback=lambda: "fallback",
            )

        self.assertEqual(choice, "fallback")
        self.assertEqual(rendered, ["1"])

    def test_render_app_screen_clears_before_printing(self) -> None:
        buffer = io.StringIO()
        content = render_screen_frame(
            path="Dev Loop > Startup",
            body=("What would you like to do?", "", "> 1. Start"),
            command_groups=(("Navigate", ("1 start",)),),
            width=40,
            height=12,
            unicode_ok=False,
        )
        with mock.patch.object(sys.stdout, "isatty", return_value=True), mock.patch(
            "builtins.print", side_effect=buffer.write
        ), mock.patch(
            "devloop.terminal_menu.prepare_terminal_output", return_value=True
        ), mock.patch.object(sys.stdout, "write", buffer.write), mock.patch.object(
            sys.stdout, "flush"
        ):
            render_app_screen(content)

        output = buffer.getvalue()
        self.assertTrue(output.startswith("\033[2J\033[H"))
        self.assertIn("Dev Loop > Startup", output)

    def test_clear_terminal_screen_is_noop_when_not_a_tty(self) -> None:
        buffer = io.StringIO()
        with mock.patch.object(sys.stdout, "isatty", return_value=False), mock.patch.object(
            sys.stdout, "write", buffer.write
        ):
            clear_terminal_screen()

        self.assertEqual(buffer.getvalue(), "")

    def test_navigation_reader_decodes_arrows_and_function_keys(self) -> None:
        up = read_navigation_key(IteratorKeySource(iter("\x1b[A")))
        f7 = read_navigation_key(IteratorKeySource(iter("\x1b[18~")))
        posix_f10 = read_navigation_key(IteratorKeySource(iter("\x1b[21~")))
        windows_f10 = read_navigation_key(IteratorKeySource(iter("\x00D")))
        escape = read_navigation_key(IteratorKeySource(iter("\x1b")))

        self.assertEqual(up, KeyEvent(NavigationKey.UP))
        self.assertEqual(f7, KeyEvent(NavigationKey.F7))
        self.assertEqual(posix_f10, KeyEvent(NavigationKey.F10))
        self.assertEqual(windows_f10, KeyEvent(NavigationKey.F10))
        self.assertEqual(escape, KeyEvent(NavigationKey.ESCAPE))

    def test_choice_menu_uses_arrows_and_enter_on_interactive_terminal(self) -> None:
        rendered: list[str] = []
        keys = IteratorKeySource(iter("\x1b[B\r"))
        with mock.patch(
            "devloop.terminal_menu._open_navigation_source",
            return_value=keys,
        ):
            choice = choose_menu_option(
                (("1", "Start"), ("2", "Resume")),
                default_key="1",
                render=rendered.append,
                fallback=lambda: "fallback",
            )

        self.assertEqual(choice, "2")
        self.assertEqual(rendered, ["1", "2"])

        for sequence in ("\x1b",):
            with self.subTest(sequence=repr(sequence)), mock.patch(
                "devloop.terminal_menu._open_navigation_source",
                return_value=IteratorKeySource(iter(sequence)),
            ):
                choice = choose_menu_option(
                    (("1", "Start"), ("q", "Exit")),
                    default_key="1",
                    render=lambda _selected: None,
                    fallback=lambda: "fallback",
                    cancel_key="q",
                )
            self.assertEqual(choice, "q")

    def test_workflow_reader_maps_arrow_and_action_palette_selection(self) -> None:
        actions = (
            MenuAction("View", "Route map", "graph"),
            MenuAction("Finish", "Apply", "apply"),
        )
        with mock.patch(
            "devloop.terminal_menu._open_navigation_source",
            return_value=IteratorKeySource(iter("\x1b[A")),
        ):
            command = read_workflow_command(
                "workflow> ",
                fallback=lambda _prompt: "fallback",
                actions=actions,
            )

        self.assertEqual(command, "__previous_step__")

        with mock.patch(
            "devloop.terminal_menu._open_navigation_source",
            return_value=IteratorKeySource(iter("\r\x1b[B\r\r")),
        ), mock.patch("devloop.terminal_menu.render_app_screen"):
            command = read_workflow_command(
                "workflow> ",
                fallback=lambda _prompt: "fallback",
                actions=actions,
            )

        self.assertEqual(command, "apply")

        with mock.patch(
            "devloop.terminal_menu._open_navigation_source",
            return_value=IteratorKeySource(iter("\x1b")),
        ):
            command = read_workflow_command(
                "workflow> ",
                fallback=lambda _prompt: "fallback",
                actions=actions,
            )

        self.assertEqual(command, "cancel")

        with mock.patch(
            "devloop.terminal_menu._open_navigation_source",
            return_value=IteratorKeySource(iter("\r\x1b[F\r")),
        ), mock.patch("devloop.terminal_menu.render_app_screen"):
            command = read_workflow_command(
                "workflow> ",
                fallback=lambda _prompt: "fallback",
                actions=actions,
            )

        self.assertEqual(command, "")

    def test_workflow_reader_uses_line_fallback_without_navigation_source(self) -> None:
        with mock.patch(
            "devloop.terminal_menu._open_navigation_source",
            return_value=None,
        ):
            command = read_workflow_command(
                "workflow> ",
                fallback=lambda prompt: f"fallback:{prompt}",
                actions=(),
            )

        self.assertEqual(command, "fallback:workflow> ")

    @unittest.skipUnless(os.name == "posix", "PTY regression coverage is POSIX-only")
    def test_posix_arrow_sequence_keeps_process_inside_selection_menu(self) -> None:
        import pty

        child_code = """
from devloop.terminal_menu import choose_menu_option
from devloop.terminal_editor import TerminalEditor

result = choose_menu_option(
    (("1", "Start"), ("2", "Resume")),
    default_key="1",
    cancel_key="q",
    render=lambda selected: print(f"SELECT:{selected}", flush=True),
    fallback=lambda: "fallback",
)
print(f"RESULT:{result}", flush=True)
line = TerminalEditor(
    on_paste_image=lambda: None,
    fallback_hint=None,
).read_line("TEXT:")
print(f"TEXT_RESULT:{line}", flush=True)
escape_result = choose_menu_option(
    (("1", "Start"), ("q", "Exit")),
    default_key="1",
    cancel_key="q",
    render=lambda selected: print(f"ESC_SELECT:{selected}", flush=True),
    fallback=lambda: "fallback",
)
print(f"ESC_RESULT:{escape_result}", flush=True)
"""
        master_fd, slave_fd = pty.openpty()
        environment = dict(os.environ)
        environment.pop("DEVLOOP_EDITOR", None)
        project_root = Path(__file__).resolve().parents[1]
        environment["PYTHONPATH"] = str(project_root / "src")
        process = subprocess.Popen(
            [sys.executable, "-c", child_code],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            env=environment,
            cwd=project_root,
        )
        os.close(slave_fd)

        def read_until(expected: bytes, timeout_seconds: float = 1.0) -> bytes:
            output = bytearray()
            deadline = time.monotonic() + timeout_seconds
            while expected not in output and time.monotonic() < deadline:
                readable, _writable, _errors = select.select(
                    [master_fd],
                    [],
                    [],
                    max(0.0, deadline - time.monotonic()),
                )
                if not readable:
                    continue
                try:
                    output.extend(os.read(master_fd, 4096))
                except OSError:
                    break
            return bytes(output)

        try:
            initial = read_until(b"SELECT:1")
            self.assertIn(b"SELECT:1", initial)

            os.write(master_fd, b"\x1b[B")
            moved = read_until(b"SELECT:2")
            self.assertIn(b"SELECT:2", moved, moved.decode(errors="replace"))
            self.assertIsNone(process.poll(), "menu returned control to the shell")

            os.write(master_fd, b"\r")
            completed = read_until(b"RESULT:2")
            self.assertIn(b"RESULT:2", completed)

            if b"TEXT:" not in completed:
                text_prompt = read_until(b"TEXT:")
                self.assertIn(b"TEXT:", text_prompt)
            os.write(master_fd, "γειά\r".encode())
            unicode_input = read_until("TEXT_RESULT:γειά".encode())
            self.assertIn("TEXT_RESULT:γειά".encode(), unicode_input)

            escape_ready = unicode_input
            if b"ESC_SELECT:1" not in escape_ready:
                escape_ready += read_until(b"ESC_SELECT:1")
            self.assertIn(b"ESC_SELECT:1", escape_ready)
            os.write(master_fd, b"\x1b")
            escape_result = read_until(b"ESC_RESULT:q")
            self.assertIn(b"ESC_RESULT:q", escape_result)

        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=1)
            os.close(master_fd)


if __name__ == "__main__":
    unittest.main()
