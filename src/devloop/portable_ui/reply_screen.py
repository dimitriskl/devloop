from __future__ import annotations

import asyncio
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from ..clipboard import capture_clipboard_image, read_windows_clipboard
from ..issue_reply import IssueReply, ReplyAction, pasted_file_paths, validate_attachment


class ReplyTextArea(TextArea):
    def __init__(self, text: str, attach_files: Callable[[tuple[Path, ...]], None]) -> None:
        super().__init__(text, id="reply-body")
        self._attach_files = attach_files

    async def _on_paste(self, event: events.Paste) -> None:
        event.stop()
        event.prevent_default()
        if paths := pasted_file_paths(event.text):
            self._attach_files(paths)
            return
        await super()._on_paste(event)


class IssueReplyScreen(ModalScreen[str]):
    """A reply stays in this screen until the operator submits or cancels it."""

    BINDINGS = [
        Binding("escape", "cancel_reply", "Cancel", priority=True),
        Binding("ctrl+enter", "submit_reply", "Submit", priority=True),
        Binding("alt+v", "paste_attachment", "Paste image/files", priority=True),
        Binding("ctrl+v", "paste_attachment", "Paste", priority=True),
    ]
    DEFAULT_CSS = """
    IssueReplyScreen { background: #000000; color: #ffffff; padding: 1; }
    #reply-frame {
        border: solid #ffffff; padding: 0 1; height: 100%;
        layout: grid; grid-size: 1; grid-rows: 1 1fr 2fr 3 6 1 2;
    }
    #reply-question-scroll { height: 1fr; min-height: 3; }
    #reply-body { height: 2fr; min-height: 4; }
    #reply-bottom { height: 6; }
    #reply-actions { width: 1fr; }
    #reply-attachments { width: 1fr; }
    #reply-file { height: 3; }
    #reply-error { height: auto; max-height: 3; color: #ffffff; text-style: bold; }
    #reply-hints { height: 2; }
    """

    def __init__(self, prompt: str, initial_value: str = "") -> None:
        super().__init__()
        self.prompt = prompt
        self.draft = IssueReply.decode(initial_value) if initial_value else IssueReply()
        self.paths = list(self.draft.attachments)
        self._pasting = False

    def compose(self) -> ComposeResult:
        with Vertical(id="reply-frame"):
            yield Static("Dev Loop > Reply to issue", markup=False)
            with VerticalScroll(id="reply-question-scroll"):
                yield Static(self.prompt, markup=False)
            yield ReplyTextArea(self.draft.text, self._try_add_attachments)
            yield Input(
                placeholder="Attach a file: paste its full path, then Enter", id="reply-file",
            )
            with Horizontal(id="reply-bottom"):
                yield OptionList(
                    Option("Submit reply and continue", id=ReplyAction.SUBMIT.value),
                    Option("Paste image / copied files", id=ReplyAction.PASTE.value),
                    Option("Attach file by path", id=ReplyAction.FILE.value),
                    Option("Cancel", id=ReplyAction.CANCEL.value), id="reply-actions",
                )
                yield OptionList(id="reply-attachments")
            yield Static("", id="reply-error", markup=False)
            yield Static(
                "Enter: new line | Tab: actions | Ctrl+Enter: submit | Esc: cancel\n"
                "Alt+V: paste | 6000 chars | 16 files, 25 MiB each | Enter: remove file",
                id="reply-hints",
            )

    def on_mount(self) -> None:
        self._refresh_attachments()
        self.query_one("#reply-body", TextArea).focus()

    def _refresh_attachments(self) -> None:
        menu = self.query_one("#reply-attachments", OptionList)
        menu.clear_options()
        for index, path in enumerate(self.paths):
            menu.add_option(Option(Text(f"Remove: {path.name}"), id=str(index)))

    def _error(self, message: str) -> None:
        self.query_one("#reply-error", Static).update(message)

    def _add_attachments(self, paths: tuple[Path, ...]) -> None:
        additions = [validate_attachment(path) for path in paths]
        combined = list(dict.fromkeys([*self.paths, *additions]))
        IssueReply(self.query_one("#reply-body", TextArea).text, tuple(combined)).encode()
        self.paths = combined
        self._refresh_attachments()
        self._error("")

    def _try_add_attachments(self, paths: tuple[Path, ...]) -> None:
        try:
            self._add_attachments(paths)
        except (OSError, ValueError) as error:
            self._error(str(error))

    @on(Input.Submitted, "#reply-file")
    def attach_path(self, event: Input.Submitted) -> None:
        event.stop()
        try:
            value = event.value.strip().strip('"').strip("'")
            if not value:
                raise ValueError("Paste a file path first.")
            self._add_attachments((Path(value),))
        except (OSError, ValueError) as error:
            self._error(str(error))
            return
        event.input.value = ""
        self.query_one("#reply-body", TextArea).focus()

    @on(OptionList.OptionSelected, "#reply-attachments")
    def remove_attachment(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id is not None:
            self.paths.pop(int(event.option.id))
            self._refresh_attachments()

    @on(OptionList.OptionSelected, "#reply-actions")
    async def select_action(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id == ReplyAction.SUBMIT.value:
            self.action_submit_reply()
        elif event.option.id == ReplyAction.CANCEL.value:
            self.action_cancel_reply()
        elif event.option.id == ReplyAction.FILE.value:
            self.query_one("#reply-file", Input).focus()
        elif event.option.id == ReplyAction.PASTE.value:
            await self.action_paste_attachment()

    async def action_paste_attachment(self) -> None:
        if self._pasting:
            return
        self._pasting = True
        focus = self.focused
        try:
            # Keep captures until the worker has copied them into the durable reply.
            directory = Path(tempfile.gettempdir()) / "devloop-reply-clipboard"
            image = await asyncio.to_thread(capture_clipboard_image, directory)
            if not self.is_mounted:
                return
            if image is not None:
                self._add_attachments((image,))
                return
            files, text = await asyncio.to_thread(read_windows_clipboard)
            if not self.is_mounted:
                return
            if files:
                self._add_attachments(files)
                return
            if text:
                # Copy-as-path preserves spaces. Ordinary prose goes into the draft.
                if paths := pasted_file_paths(text):
                    self._add_attachments(paths)
                elif isinstance(focus, Input):
                    focus.insert_text_at_cursor(text)
                else:
                    self.query_one("#reply-body", TextArea).insert(text)
                return
            self._error("No image or copied files found. Paste text or enter a file path.")
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            if self.is_mounted:
                self._error(str(error))
        finally:
            self._pasting = False

    def action_submit_reply(self) -> None:
        if self._pasting:
            self._error("Wait for the clipboard attachment to finish.")
            return
        try:
            reply = IssueReply(self.query_one("#reply-body", TextArea).text, tuple(self.paths))
            if not reply.text.strip() and not reply.attachments:
                raise ValueError("Type a reply or attach a file before submitting.")
            for path in reply.attachments:
                validate_attachment(path)
            self.dismiss(reply.encode())
        except (OSError, ValueError) as error:
            self._error(str(error))

    def action_cancel_reply(self) -> None:
        self.dismiss("")
