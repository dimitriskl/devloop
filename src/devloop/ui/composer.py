from __future__ import annotations

from textual.binding import Binding
from textual.message import Message
from textual.widgets import TextArea


class Composer(TextArea):
    """Shared multiline text composer for interactive Dev Loop surfaces."""

    BINDINGS = [
        Binding("ctrl+enter", "submit", show=False),
        Binding("ctrl+p", "previous_history", show=False),
        Binding("ctrl+n", "next_history", show=False),
    ]

    DEFAULT_CSS = """
    Composer {
        height: 8;
        min-height: 4;
        border: round $accent;
        padding: 0 1;
    }

    Composer:focus {
        border: round $accent-lighten-1;
    }
    """

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(
        self,
        text: str = "",
        *,
        id: str | None = None,
        widget_id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        if id is not None and widget_id is not None:
            raise ValueError("Composer accepts either id or widget_id, not both.")
        super().__init__(
            text,
            soft_wrap=True,
            show_line_numbers=False,
            id=id or widget_id,
            classes=classes,
            disabled=disabled,
        )
        self._history: list[str] = []
        self._history_index = 0

    def action_submit(self) -> None:
        text = self.text.strip()
        if not text:
            return
        self._history.append(text)
        self._history_index = len(self._history)
        self.post_message(self.Submitted(text))
        self.load_text("")

    def action_previous_history(self) -> None:
        if not self._history:
            return
        self._history_index = max(0, self._history_index - 1)
        self._load_history_text(self._history[self._history_index])

    def action_next_history(self) -> None:
        if not self._history:
            return
        self._history_index = min(len(self._history), self._history_index + 1)
        text = (
            "" if self._history_index == len(self._history) else self._history[self._history_index]
        )
        self._load_history_text(text)

    def _load_history_text(self, text: str) -> None:
        self.load_text(text)
        lines = text.split("\n")
        self.move_cursor((len(lines) - 1, len(lines[-1])))
