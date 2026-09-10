from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

from textual import events
from textual.widgets import TextArea

from devloop.clipboard import _default_runner, read_windows_clipboard
from devloop.issue_reply import IssueReply, issue_reply_context, save_issue_reply
from devloop.portable_runtime import PortableRuntimeBridge
from devloop.portable_ui.app import PortableApplicationShell
from devloop.portable_ui.reply_screen import IssueReplyScreen


def test_windows_file_drop_keeps_unicode_and_spaces_and_bounds_subprocess() -> None:
    files = [r"C:\Files\Greek Ελληνικά.png", r"C:\Files\notes file.pdf"]
    runner = Mock(return_value=subprocess.CompletedProcess(
        [], 0, json.dumps({"files": files, "text": ""}).encode("utf-8"), b"",
    ))
    paths, text = read_windows_clipboard(runner=runner, platform_name="win32")
    assert [str(path) for path in paths] == files
    assert text == ""
    command = runner.call_args.args[0]
    assert "-STA" in command
    assert "GetFileDropList" in command[-1]
    with patch("devloop.clipboard.subprocess.run") as run:
        _default_runner(command)
    assert run.call_args.kwargs["stdin"] == subprocess.DEVNULL
    assert run.call_args.kwargs["timeout"] == 10


def test_clipboard_image_files_and_terminal_text_survive_to_saved_reply(tmp_path: Path) -> None:
    async def check() -> None:
        screenshot = tmp_path / "clipboard.png"
        screenshot.write_bytes(b"screenshot")
        evidence = tmp_path / "notes [literal] Ελληνικά.txt"
        evidence.write_text("evidence", encoding="utf-8")
        received = Mock()
        app = PortableApplicationShell(PortableRuntimeBridge(), operation=lambda: 0)
        async with app.run_test(size=(100, 32)) as pilot:
            screen = IssueReplyScreen("Question")
            app.push_screen(screen, received)
            await pilot.pause()
            editor = screen.query_one("#reply-body", TextArea)
            editor.post_message(events.Paste("first line\nsecond line"))
            await pilot.pause()
            assert editor.text == "first line\nsecond line"
            with patch(
                "devloop.portable_ui.reply_screen.capture_clipboard_image",
                return_value=screenshot,
            ):
                await screen.action_paste_attachment()
            assert screen.paths == [screenshot]
            with patch(
                "devloop.portable_ui.reply_screen.capture_clipboard_image", return_value=None,
            ), patch(
                "devloop.portable_ui.reply_screen.read_windows_clipboard",
                return_value=((evidence,), ""),
            ):
                await screen.action_paste_attachment()
            assert screen.paths == [screenshot, evidence]
            # Windows Terminal sends pasted text as Paste rather than Ctrl+V.
            editor.post_message(events.Paste(f'"{evidence}"'))
            await pilot.pause()
            assert screen.paths == [screenshot, evidence]
            assert editor.text == "first line\nsecond line"
            await pilot.press("ctrl+enter")
            await pilot.pause()
        reply = IssueReply.decode(received.call_args.args[0])
        save_issue_reply(tmp_path / "logs", "0004", reply)
        context, images = issue_reply_context(tmp_path / "logs", "0004")
        assert "first line\nsecond line" in context
        assert "notes [literal] Ελληνικά.txt" in context
        assert images[0].read_bytes() == b"screenshot"
    asyncio.run(check())


def test_rejected_pasted_attachment_does_not_erase_text(tmp_path: Path) -> None:
    async def check() -> None:
        app = PortableApplicationShell(PortableRuntimeBridge(), operation=lambda: 0)
        async with app.run_test() as pilot:
            screen = IssueReplyScreen("Question", IssueReply("Original answer").encode())
            app.push_screen(screen)
            await pilot.pause()
            with patch(
                "devloop.portable_ui.reply_screen.capture_clipboard_image", return_value=None,
            ), patch(
                "devloop.portable_ui.reply_screen.read_windows_clipboard",
                return_value=((tmp_path / "missing.pdf",), ""),
            ):
                await screen.action_paste_attachment()
            assert screen.paths == []
            assert screen.query_one("#reply-body", TextArea).text == "Original answer"
    asyncio.run(check())
