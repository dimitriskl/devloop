from __future__ import annotations

import os
from collections.abc import Callable

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from ..logo import render_logo
from ..portable_presentation import (
    PortableActivity,
    PortableActivityFeed,
    PortableActivityStatus,
)
from ..portable_runtime import (
    PortableRunContext,
    PortableRuntimeBridge,
    PortableRuntimeEvent,
    PortableRuntimeEventKind,
    PortableRuntimeStopped,
    portable_runtime_session,
    route_worker_output,
)
from ..run_review import REVIEW_SCREEN_PATH, REVIEW_SUCCESS_HEADING
from ..subprocess_utils import terminate_active_process_trees
from ..terminal_text import sanitize_terminal_text
from ..version import VERSION


MINIMUM_TERMINAL_COLUMNS = 80
MINIMUM_TERMINAL_ROWS = 24
DEFAULT_ACTION_BAR = (
    "F1 Help | F2 Primary | F3 View | F4 Logs | F5 Context | "
    "F9 Actions | Esc Back"
)
SELECTION_ACTION_BAR = f"Enter Select | {DEFAULT_ACTION_BAR}"
CAPTURED_ACTIVITY_TITLE = "Captured Activity"
COMPLETION_REVIEW_LOG_TITLE = "Completion Review and Captured Activity"
RUN_CONTEXT_TITLE = "Run context"


class PortableDetail(Static):
    def __init__(
        self,
        content: str,
        *,
        report_content_size: Callable[[int, int], None],
        **kwargs: object,
    ) -> None:
        super().__init__(content, **kwargs)
        self._report_content_size = report_content_size

    def on_resize(self, _event: events.Resize) -> None:
        size = self.content_region.size
        self._report_content_size(size.width, size.height)


class PortableTextOverlay(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "Close", show=False)]

    def __init__(self, title: str, content: str) -> None:
        super().__init__()
        self._title = title
        self._content = content

    def compose(self) -> ComposeResult:
        with Vertical(classes="portable-overlay"):
            yield Static(self._title, classes="portable-overlay-title")
            yield Static(
                self._content,
                classes="portable-overlay-content",
                markup=False,
            )
            yield Static("Esc Close", classes="portable-overlay-actions")

    def action_close(self) -> None:
        self.dismiss()


class PortableLogOverlay(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "Close", show=False)]

    def __init__(
        self,
        lines: tuple[str, ...],
        *,
        title: str = CAPTURED_ACTIVITY_TITLE,
    ) -> None:
        super().__init__()
        self._lines = lines
        self._title = title

    def compose(self) -> ComposeResult:
        with Vertical(classes="portable-overlay"):
            yield Static(self._title, classes="portable-overlay-title")
            yield Input(placeholder="Filter logs", id="portable-log-filter")
            yield RichLog(
                id="portable-log-content",
                wrap=True,
                markup=False,
                max_lines=1000,
            )
            yield Static(
                "Type Filter | Tab Scroll | Esc Close",
                classes="portable-overlay-actions",
            )

    def on_mount(self) -> None:
        self._refresh("")
        self.query_one("#portable-log-filter", Input).focus()

    @on(Input.Changed, "#portable-log-filter")
    def filter_logs(self, event: Input.Changed) -> None:
        self._refresh(event.value)

    def _refresh(self, query: str) -> None:
        log = self.query_one("#portable-log-content", RichLog)
        log.clear()
        normalized_query = query.casefold().strip()
        matching = (
            line
            for line in self._lines
            if not normalized_query or normalized_query in line.casefold()
        )
        found = False
        for line in matching:
            found = True
            log.write(line)
        if not found:
            log.write("No matching captured output.")

    def action_close(self) -> None:
        self.dismiss()


class PortableApplicationShell(App[None]):
    CSS = """
    Screen {
        background: #012456;
        color: #f4f7fb;
    }

    #portable-shell {
        width: 100%;
        height: 100%;
        border: solid #3a96dd;
        background: #012456;
    }

    #portable-header {
        height: 1;
        padding: 0 1;
        background: #0b5d7a;
        color: #ffffff;
        text-style: bold;
    }

    #portable-body {
        height: 1fr;
    }

    #portable-left-pane {
        width: 35%;
        min-width: 28;
        max-width: 42;
        border-right: solid #3a96dd;
    }

    #portable-right-pane {
        width: 1fr;
    }

    .portable-pane-title {
        height: 1;
        padding: 0 1;
        background: #073763;
        color: #9cdcfe;
        text-style: bold;
    }

    #portable-navigation {
        height: 1fr;
        background: #012456;
        color: #f4f7fb;
    }

    #portable-navigation > .option-list--option-highlighted {
        background: #2b579a;
        color: #ffffff;
        text-style: bold;
    }

    #portable-run-context-title {
        display: none;
    }

    #portable-run-context {
        display: none;
        height: auto;
        max-height: 12;
        padding: 0 1 1 1;
        overflow-y: auto;
        border-bottom: solid #3a96dd;
        background: #001b3d;
    }

    #portable-detail {
        height: 2fr;
        padding: 0 1;
        overflow-y: auto;
        border-bottom: solid #3a96dd;
    }

    #portable-activity {
        height: 1fr;
        padding: 0 1;
        background: #001b3d;
    }

    #portable-input {
        display: none;
        height: 3;
        margin: 0 1;
        border: solid #3a96dd;
    }

    #portable-status {
        height: 1;
        padding: 0 1;
        background: #0b5d7a;
        color: #ffffff;
        text-style: bold;
    }

    #portable-actions {
        height: 1;
        padding: 0 1;
        background: #081b2c;
        color: #f4f7fb;
    }

    #portable-size-warning {
        display: none;
        layer: warning;
        width: 100%;
        height: 100%;
        padding: 2 4;
        background: #000000;
        color: #ffff00;
        text-align: center;
        content-align: center middle;
        text-style: bold;
    }

    PortableTextOverlay {
        align: center middle;
        background: #000000 60%;
    }

    .portable-monochrome,
    .portable-monochrome #portable-shell,
    .portable-monochrome #portable-left-pane,
    .portable-monochrome #portable-detail,
    .portable-monochrome #portable-input,
    .portable-monochrome .portable-overlay {
        background: #000000;
        color: #ffffff;
        border: solid #ffffff;
    }

    .portable-overlay {
        width: 82%;
        height: 76%;
        border: solid #3a96dd;
        background: #012456;
    }

    .portable-overlay-title {
        height: 1;
        padding: 0 1;
        background: #0b5d7a;
        color: #ffffff;
        text-style: bold;
    }

    .portable-overlay-content {
        height: 1fr;
        padding: 1 2;
        overflow-y: auto;
    }

    #portable-log-filter {
        height: 3;
        margin: 0 1;
        border: solid #3a96dd;
    }

    #portable-log-content {
        height: 1fr;
        padding: 0 1;
    }

    .portable-overlay-actions {
        height: 1;
        padding: 0 1;
        background: #081b2c;
    }
    """

    BINDINGS = [
        Binding("f1", "help", "Help", show=False),
        Binding("f2", "primary", "Primary", show=False),
        Binding("f3", "view", "View", show=False),
        Binding("f4", "logs", "Logs", show=False),
        Binding("f5", "context", "Context", show=False),
        Binding("f9", "actions", "Actions", show=False),
        Binding("alt+v", "paste_image", "Paste image", show=False),
        Binding("escape", "back", "Back", show=False),
        Binding("ctrl+c", "request_stop", "Stop", show=False, priority=True),
    ]

    def __init__(
        self,
        bridge: PortableRuntimeBridge,
        operation: Callable[[], int],
    ) -> None:
        super().__init__()
        self._bridge = bridge
        self._operation = operation
        self._active_request_id: int | None = None
        self._cancel_key: str | None = None
        self.operation_result: int | None = None
        self._workflow_complete = False
        self._captured_output: list[str] = []
        self._completion_review_content: str | None = None
        self._run_context: PortableRunContext | None = None
        self._activity_feed = PortableActivityFeed()
        self._latest_screen_content = render_logo().rstrip()
        self._input_history: tuple[str, ...] = ()
        self._input_history_position = 0
        self._shortcut_commands: dict[str, str] = {}
        self._option_labels: dict[str, str] = {}
        self._pending_working_state: tuple[int, str] | None = None
        self._terminal_too_small = False
        self._operation_stop_requested = False

    def compose(self) -> ComposeResult:
        with Vertical(id="portable-shell"):
            yield Static(
                f"DEV LOOP v{VERSION}",
                id="portable-header",
                markup=False,
            )
            with Horizontal(id="portable-body"):
                with Vertical(id="portable-left-pane"):
                    yield Static(
                        RUN_CONTEXT_TITLE,
                        id="portable-run-context-title",
                        classes="portable-pane-title",
                    )
                    yield Static(
                        "",
                        id="portable-run-context",
                        markup=False,
                    )
                    yield Static("Navigation", classes="portable-pane-title")
                    yield OptionList(id="portable-navigation")
                with Vertical(id="portable-right-pane"):
                    yield Static("Selected details", classes="portable-pane-title")
                    yield PortableDetail(
                        render_logo().rstrip(),
                        report_content_size=self._bridge.set_content_size,
                        id="portable-detail",
                        markup=False,
                    )
                    yield Static("Activity", classes="portable-pane-title")
                    yield RichLog(
                        id="portable-activity",
                        wrap=True,
                        markup=False,
                        max_lines=100,
                    )
                    yield Input(id="portable-input")
            yield Static("STARTING", id="portable-status")
            yield Static(
                DEFAULT_ACTION_BAR,
                id="portable-actions",
            )
            yield Static("", id="portable-size-warning")

    def on_mount(self) -> None:
        if os.environ.get("NO_COLOR"):
            self.screen.add_class("portable-monochrome")
        self._sync_runtime_content_size()
        self.call_after_refresh(self._sync_runtime_content_size)
        self.set_interval(0.02, self._drain_runtime_events)
        self.run_worker(
            self._execute_operation,
            thread=True,
            exclusive=True,
            name="portable-workflow",
        )

    def on_resize(self, event: events.Resize) -> None:
        too_small = (
            event.size.width < MINIMUM_TERMINAL_COLUMNS
            or event.size.height < MINIMUM_TERMINAL_ROWS
        )
        self._terminal_too_small = too_small
        self.query_one("#portable-body", Horizontal).disabled = too_small
        warning = self.query_one("#portable-size-warning", Static)
        warning.display = too_small
        if too_small:
            warning.update(
                "Terminal too small\n\n"
                f"Current: {event.size.width}x{event.size.height}\n"
                f"Required: {MINIMUM_TERMINAL_COLUMNS}x{MINIMUM_TERMINAL_ROWS}\n\n"
                "Resize the terminal to continue."
            )
        self.call_after_refresh(self._sync_runtime_content_size)

    def on_unmount(self) -> None:
        self._stop_operation()

    def _stop_operation(self) -> None:
        if self._operation_stop_requested:
            return
        self._operation_stop_requested = True
        self._active_request_id = None
        self._cancel_key = None
        if self.operation_result is None:
            self.operation_result = 130
        self._bridge.request_stop()
        terminate_active_process_trees()

    def _sync_runtime_content_size(self) -> None:
        size = self.query_one("#portable-detail", Static).content_region.size
        self._bridge.set_content_size(size.width, size.height)

    def _execute_operation(self) -> None:
        try:
            with portable_runtime_session(self._bridge):
                result = self._operation()
        except SystemExit as error:
            result = error.code if isinstance(error.code, int) else 1
            self.call_from_thread(self._operation_finished, result)
            return
        except KeyboardInterrupt:
            self.call_from_thread(self._operation_finished, 130)
            return
        except PortableRuntimeStopped:
            if self.operation_result is None:
                self.operation_result = 130
            return
        except Exception as error:
            self.call_from_thread(self._operation_failed, error)
            return
        self.call_from_thread(self._operation_finished, result)

    def _operation_finished(self, result: int) -> None:
        self._drain_runtime_events()
        self.operation_result = result
        self._workflow_complete = True
        if self._latest_screen_content.lstrip().startswith(REVIEW_SCREEN_PATH):
            self.query_one("#portable-detail", Static).update(
                self._latest_screen_content
            )
            self.query_one("#portable-header", Static).update(REVIEW_SCREEN_PATH)
            review_status = (
                "WORKFLOW FINISHED · SUCCESS"
                if REVIEW_SUCCESS_HEADING in self._latest_screen_content
                else "WORKFLOW FINISHED · ATTENTION REQUIRED"
            )
            self.query_one("#portable-status", Static).update(review_status)
            self._show_exit_action()
            return
        outcome = (
            "Completed successfully"
            if result == 0
            else f"Finished with exit code {result}"
        )
        self.query_one("#portable-detail", Static).update(
            "Dev Loop > Final Result\n\n"
            f"Outcome: {outcome}\n\n"
            "Last workflow view\n"
            "------------------\n"
            f"{self._latest_screen_content}\n\n"
            "Use F4 for captured activity and output."
        )
        self.query_one("#portable-header", Static).update("Dev Loop > Final Result")
        self.query_one("#portable-status", Static).update(
            "COMPLETED" if result == 0 else f"FINISHED · EXIT {result}"
        )
        self._show_exit_action()

    def _operation_failed(self, error: Exception) -> None:
        self._drain_runtime_events()
        self.operation_result = 1
        self._workflow_complete = True
        self.query_one("#portable-status", Static).update("FAILED")
        safe_error = sanitize_terminal_text(error, preserve_newlines=False)
        self.query_one("#portable-detail", Static).update(
            f"{type(error).__name__}: {safe_error}"
        )
        self._show_exit_action()

    def _show_exit_action(self) -> None:
        menu = self.query_one("#portable-navigation", OptionList)
        menu.display = True
        menu.disabled = False
        menu.clear_options()
        menu.add_option(Option("Exit Dev Loop", id="__exit__"))
        menu.highlighted = 0
        menu.focus()
        self.query_one("#portable-actions", Static).update(
            "Enter Exit | F4 Logs | F5 Context"
        )

    def _drain_runtime_events(self) -> None:
        while True:
            event = self._bridge.try_next_event()
            if event is None:
                return
            self._handle_runtime_event(event)

    def _handle_runtime_event(self, event: PortableRuntimeEvent) -> None:
        if event.kind is PortableRuntimeEventKind.CHOICE_REQUESTED:
            self._show_choice(event)
        elif event.kind is PortableRuntimeEventKind.INPUT_REQUESTED:
            self._show_input(event)
        elif event.kind is PortableRuntimeEventKind.INTERACTION_COMPLETED:
            self._finish_interaction_transition(event.request_id)
        elif event.kind is PortableRuntimeEventKind.RUN_CONTEXT_UPDATED:
            if event.run_context is not None:
                self._update_run_context(event.run_context)
        elif event.kind is PortableRuntimeEventKind.SCREEN_UPDATED:
            safe_content = sanitize_terminal_text(
                event.content,
                preserve_newlines=True,
            )
            self._latest_screen_content = safe_content
            if safe_content.lstrip().startswith(REVIEW_SCREEN_PATH):
                self._completion_review_content = safe_content
            self.query_one("#portable-detail", Static).update(safe_content)
            heading = next(
                (line.strip() for line in safe_content.splitlines() if line.strip()),
                "Dev Loop",
            )
            self.query_one("#portable-header", Static).update(heading[:120])
        elif event.kind is PortableRuntimeEventKind.OUTPUT_WRITTEN:
            content = event.content.rstrip("\r\n")
            if content:
                prefix = "ERROR · " if event.is_error else ""
                rendered = prefix + sanitize_terminal_text(
                    content,
                    preserve_newlines=True,
                )
                self._captured_output.append(rendered)
                del self._captured_output[:-500]
                self._publish_activity(rendered)

    def _update_run_context(self, context: PortableRunContext) -> None:
        self._run_context = PortableRunContext(
            project_root=sanitize_terminal_text(
                context.project_root,
                preserve_newlines=False,
            ),
            implementation_branch=sanitize_terminal_text(
                context.implementation_branch,
                preserve_newlines=False,
            ),
            implementation_worktree=sanitize_terminal_text(
                context.implementation_worktree,
                preserve_newlines=False,
            ),
            prd_path=sanitize_terminal_text(
                context.prd_path,
                preserve_newlines=False,
            ),
        )
        context_title = self.query_one("#portable-run-context-title", Static)
        context_view = self.query_one("#portable-run-context", Static)
        context_title.display = True
        context_view.display = True
        context_view.update(self._render_compact_run_context())

    def _render_compact_run_context(self) -> str:
        assert self._run_context is not None
        return (
            f"Project: {self._run_context.project_root}\n"
            f"Branch: {self._run_context.implementation_branch}\n"
            f"Changes: {self._run_context.implementation_worktree}"
        )

    def _render_full_run_context(self) -> str:
        assert self._run_context is not None
        lines = [
            f"Project checkout:       {self._run_context.project_root}",
            f"Implementation branch:  {self._run_context.implementation_branch}",
            f"Changes are written to: {self._run_context.implementation_worktree}",
        ]
        if self._run_context.prd_path:
            lines.append(f"PRD:                    {self._run_context.prd_path}")
        return "\n".join(lines)

    def _publish_activity(self, message: str) -> None:
        normalized = message.casefold()
        if "fail" in normalized or "error" in normalized:
            status = PortableActivityStatus.FAILED
        elif any(word in normalized for word in ("finished", "pass", "succeeded")):
            status = PortableActivityStatus.SUCCEEDED
        elif any(word in normalized for word in ("running", "started", "waiting")):
            status = PortableActivityStatus.RUNNING
        else:
            status = PortableActivityStatus.NOTICE
        operation_id = message
        if message.startswith("[") and ":" in message:
            operation_id = message.split(":", 1)[0]
        if status is PortableActivityStatus.FAILED:
            operation_id = f"{operation_id}:failure:{len(self._captured_output)}"
        self._activity_feed = self._activity_feed.publish(
            PortableActivity(operation_id, message, status)
        )
        activity = self.query_one("#portable-activity", RichLog)
        activity.clear()
        for item in self._activity_feed.items:
            activity.write(f"{item.status.value:<9} {item.message}")

    def _show_choice(self, event: PortableRuntimeEvent) -> None:
        self._active_request_id = event.request_id
        self._pending_working_state = None
        self._cancel_key = event.cancel_key
        self._shortcut_commands = dict(event.shortcuts)
        self._option_labels = {
            key: sanitize_terminal_text(label, preserve_newlines=False)
            for key, label in event.options
        }
        input_widget = self.query_one("#portable-input", Input)
        input_widget.display = False
        menu = self.query_one("#portable-navigation", OptionList)
        menu.display = True
        menu.disabled = False
        menu.clear_options()
        menu.add_options(
            [
                Option(
                    sanitize_terminal_text(label, preserve_newlines=False),
                    id=key,
                )
                for key, label in event.options
            ]
        )
        highlighted = next(
            (
                index
                for index, (key, _label) in enumerate(event.options)
                if key == event.default_key
            ),
            0,
        )
        menu.highlighted = highlighted
        menu.focus()
        self.query_one("#portable-status", Static).update(
            "WAITING FOR SELECTION · ENTER TO CONFIRM"
        )
        self.query_one("#portable-actions", Static).update(SELECTION_ACTION_BAR)

    def _show_input(self, event: PortableRuntimeEvent) -> None:
        self._active_request_id = event.request_id
        self._pending_working_state = None
        self._cancel_key = ""
        self._shortcut_commands = {}
        self._input_history = event.input_history
        self._input_history_position = len(self._input_history)
        self.query_one("#portable-navigation", OptionList).display = False
        input_widget = self.query_one("#portable-input", Input)
        input_widget.placeholder = event.prompt
        input_widget.value = ""
        input_widget.display = True
        input_widget.focus()
        self.query_one("#portable-detail", Static).update(event.prompt)
        self.query_one("#portable-status", Static).update("WAITING FOR INPUT")
        self.query_one("#portable-actions", Static).update(
            "Enter Confirm | Up/Down History | Alt+V Screenshot | Esc Cancel"
        )

    def on_key(self, event: events.Key) -> None:
        input_widget = self.query_one("#portable-input", Input)
        if self.focused is not input_widget or not input_widget.display:
            return
        if event.key == "up" and self._input_history:
            self._input_history_position = max(
                0,
                self._input_history_position - 1,
            )
        elif event.key == "down" and self._input_history:
            self._input_history_position = min(
                len(self._input_history),
                self._input_history_position + 1,
            )
        else:
            return
        input_widget.value = (
            self._input_history[self._input_history_position]
            if self._input_history_position < len(self._input_history)
            else ""
        )
        input_widget.cursor_position = len(input_widget.value)
        event.prevent_default()
        event.stop()

    @on(OptionList.OptionHighlighted, "#portable-navigation")
    def preview_option(self, event: OptionList.OptionHighlighted) -> None:
        option_id = event.option.id
        if self._active_request_id is None or option_id is None:
            return
        self._bridge.preview(self._active_request_id, option_id)

    @on(OptionList.OptionSelected, "#portable-navigation")
    def select_option(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option.id
        request_id = self._active_request_id
        if self._workflow_complete and option_id == "__exit__":
            self.exit()
            return
        if request_id is None or option_id is None:
            return
        option_label = self._option_labels.get(option_id, option_id)
        self._respond(request_id, option_id, f"Accepted: {option_label}")

    @on(Input.Submitted, "#portable-input")
    def submit_input(self, event: Input.Submitted) -> None:
        request_id = self._active_request_id
        if request_id is None:
            return
        event.input.display = False
        self._respond(request_id, event.value, "Input accepted")

    def action_back(self) -> None:
        if self._active_request_id is None:
            if not self._workflow_complete:
                self._show_working_explanation()
            return
        if self._cancel_key is None:
            self.push_screen(
                PortableTextOverlay(
                    "Back Unavailable",
                    "This view has no Back or Cancel action. Choose an explicit "
                    "menu item to continue.",
                )
            )
            return
        request_id = self._active_request_id
        self.query_one("#portable-input", Input).display = False
        self._respond(
            request_id,
            self._cancel_key,
            "Back or cancel accepted",
        )

    def _respond(self, request_id: int, value: str, message: str) -> None:
        self._active_request_id = None
        self._pending_working_state = (request_id, message)
        self._show_working_state(message)
        self._bridge.respond(request_id, value)

    def _finish_interaction_transition(self, request_id: int) -> None:
        pending = self._pending_working_state
        if pending is None or pending[0] != request_id:
            return
        self._pending_working_state = None
        if not self._workflow_complete:
            self._show_working_state(pending[1])

    def _show_working_state(self, message: str) -> None:
        safe_message = sanitize_terminal_text(message, preserve_newlines=False)
        menu = self.query_one("#portable-navigation", OptionList)
        menu.clear_options()
        menu.disabled = True
        menu.display = True
        self.query_one("#portable-header", Static).update("Dev Loop > Working")
        self.query_one("#portable-detail", Static).update(
            "Dev Loop > Working\n\n"
            f"{safe_message}\n\n"
            "The application is still active. Progress or the next choice will "
            "replace this view automatically."
        )
        self.query_one("#portable-status", Static).update("WORKING")
        self.query_one("#portable-actions", Static).update(
            "F1 Help | F4 Logs | F5 Context | Ctrl+C Stop | Esc Status"
        )
        self._publish_activity(f"RUNNING · {safe_message}")

    def _show_working_explanation(self) -> None:
        self.push_screen(
            PortableTextOverlay(
                "Workflow In Progress",
                "Your last selection was already accepted and Dev Loop is working.\n\n"
                "There is no safe Back action at this moment. Progress or the next "
                "choice will appear in this application automatically.",
            )
        )

    def action_help(self) -> None:
        self.push_screen(
            PortableTextOverlay(
                "Dev Loop Help",
                "Up/Down  Choose\n"
                "Enter    Open or confirm\n"
                "Esc      Back, cancel, or close overlay\n"
                "F2       Primary action\n"
                "F4       Captured activity and output\n"
                "F5       Project, branch, worktree, and PRD context\n"
                "F9       Focus contextual actions\n"
                "Ctrl+C   Request a safe stop",
            )
        )

    def action_primary(self) -> None:
        if self._respond_to_shortcut("f2"):
            return
        menu = self.query_one("#portable-navigation", OptionList)
        if menu.display and menu.highlighted is not None:
            menu.action_select()

    def action_view(self) -> None:
        self._respond_to_shortcut("f3")

    def action_context(self) -> None:
        if self._respond_to_shortcut("f5"):
            return
        if self._run_context is not None:
            self.push_screen(
                PortableTextOverlay(
                    RUN_CONTEXT_TITLE,
                    self._render_full_run_context(),
                )
            )

    def action_logs(self) -> None:
        captured_lines = list(self._captured_output)
        if self._completion_review_content is not None:
            captured_lines.insert(0, self._completion_review_content)
        lines = tuple(captured_lines) or ("No captured output yet.",)
        title = (
            COMPLETION_REVIEW_LOG_TITLE
            if self._completion_review_content is not None
            else CAPTURED_ACTIVITY_TITLE
        )
        self.push_screen(PortableLogOverlay(lines, title=title))

    def action_actions(self) -> None:
        if self._respond_to_shortcut("f9"):
            return
        menu = self.query_one("#portable-navigation", OptionList)
        for index in range(menu.option_count):
            if menu.get_option_at_index(index).id == "actions":
                menu.highlighted = index
                menu.action_select()
                return
        menu.focus()

    def _respond_to_shortcut(self, key: str) -> bool:
        command = self._shortcut_commands.get(key)
        request_id = self._active_request_id
        if command is None or request_id is None:
            return False
        self._respond(request_id, command, f"Accepted shortcut: {key.upper()}")
        return True

    def action_paste_image(self) -> None:
        input_widget = self.query_one("#portable-input", Input)
        request_id = self._active_request_id
        if request_id is None or not input_widget.display:
            return
        input_widget.display = False
        self._respond(request_id, "/paste", "Attaching screenshot")

    def action_request_stop(self) -> None:
        if self._active_request_id is not None and self._cancel_key is not None:
            self.push_screen(
                PortableTextOverlay(
                    "Stop Current Interaction",
                    "Press Esc to keep working.\n\n"
                    "Use Esc again in the main view to choose the explicit "
                    "Back or Cancel action safely.",
                )
            )
            return
        self.push_screen(
            PortableTextOverlay(
                "Workflow Is Running",
                "There is no safe interrupt point at this moment.\n\n"
                "Dev Loop will keep the terminal application mounted and expose "
                "a Back, Cancel, or Exit action at the next safe boundary.",
            )
        )


def run_portable_application(operation: Callable[[], int]) -> int:
    """Run one portable workflow inside the persistent terminal application."""
    bridge = PortableRuntimeBridge()
    app = PortableApplicationShell(bridge, operation)
    with route_worker_output(bridge):
        app.run()
    return app.operation_result if app.operation_result is not None else 130
