"""The minimal Dev Loop shell: a scrolling activity area over one pinned input line."""
from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Input, RichLog, Static

from devloop.portable_sessions import (
    PortableSessionController,
    PortableSessionInputRequest,
    PortableSessionIntent,
    PortableSessionIntentKind,
    PortableSessionLaunch,
    PortableSessionSnapshot,
    PortableSessionStatus,
)

from .activity_stream import NOTICE, OUTPUT, PROBLEM, ActivityStream
from .guidance_store import GuidanceStore, NoActiveIssue
from .input_prompt import UnresolvedAnswer, prompt_lines, resolve_answer
from .session_target import resolve_session_target

#: Statuses where Esc or a first Ctrl+C means "stop at the next checkpoint".
PAUSABLE_STATUSES = frozenset(
    {
        PortableSessionStatus.RUNNING,
        PortableSessionStatus.WAITING_FOR_INPUT,
        PortableSessionStatus.QUEUED,
    }
)
#: Statuses where a bare Enter continues the run.
CONTINUABLE_STATUSES = frozenset(
    {
        PortableSessionStatus.READY,
        PortableSessionStatus.PAUSED,
        PortableSessionStatus.INTERRUPTED,
    }
)

_HINTS = {
    PortableSessionStatus.RUNNING: "working - esc to pause",
    PortableSessionStatus.QUEUED: "queued - esc to pause",
    PortableSessionStatus.PAUSING: "pausing at the next checkpoint",
    PortableSessionStatus.WAITING_FOR_INPUT: "answer above - esc to pause",
    PortableSessionStatus.PAUSED: "paused - enter to continue, ctrl+c to exit",
    PortableSessionStatus.INTERRUPTED: "interrupted - enter to continue, ctrl+c to exit",
    PortableSessionStatus.READY: "enter to start, ctrl+c to exit",
}

_STYLES = {OUTPUT: "", NOTICE: "dim", PROBLEM: "bold red"}

_INTERRUPTED_EXIT_CODE = 130
_LAUNCH_FAILURE_EXIT_CODE = 73


class MinimalShell(App[None]):
    """Drive exactly one Portable Workflow Session with no chrome around it."""

    CSS = """
    Screen {
        background: $background;
    }
    #activity {
        height: 1fr;
        padding: 0 1;
        background: $background;
        border: none;
        scrollbar-size-vertical: 1;
    }
    #composer {
        height: 1;
        padding: 0 1;
    }
    #chevron {
        width: 2;
        height: 1;
    }
    #entry, #entry:focus {
        width: 1fr;
        height: 1;
        border: none;
        padding: 0;
        background: $background;
    }
    #hint {
        height: 1;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "pause", "Pause", show=False, priority=True),
        Binding("ctrl+c", "interrupt", "Pause or exit", show=False, priority=True),
    ]

    ENABLE_COMMAND_PALETTE = False

    def __init__(
        self,
        *,
        supervisor: PortableSessionController,
        launch: PortableSessionLaunch,
        guidance: GuidanceStore,
        prd_path: Path,
        poll_interval: float = 0.05,
    ) -> None:
        super().__init__()
        self._supervisor = supervisor
        self._launch = launch
        self._guidance = guidance
        self._prd_path = prd_path
        self._poll_interval = poll_interval
        self._stream = ActivityStream()
        self._session_id: str | None = None
        self._snapshot: PortableSessionSnapshot | None = None
        self._shown_request: tuple[str, int] | None = None
        self._finished = False
        self.exit_code = _INTERRUPTED_EXIT_CODE

    # ---- composition -------------------------------------------------

    def compose(self) -> ComposeResult:
        yield RichLog(id="activity", wrap=True, markup=False, highlight=False)
        with Horizontal(id="composer"):
            yield Static(">", id="chevron")
            yield Input(id="entry")
        yield Static("", id="hint")

    def on_mount(self) -> None:
        self.query_one("#entry", Input).focus()
        self.set_interval(self._poll_interval, self._drain)
        self._start()

    def on_unmount(self) -> None:
        self._supervisor.shutdown()

    # ---- session lifecycle -------------------------------------------

    def _start(self) -> None:
        target = resolve_session_target(
            self._supervisor.list_sessions(),
            new_session_id=self._launch.session_id,
            prd_path=self._prd_path,
        )
        self._session_id = target.session_id
        if target.resume:
            self._write(
                f"Continuing the session already recorded for {self._prd_path.name}.",
                NOTICE,
            )
            intent = PortableSessionIntent(
                kind=PortableSessionIntentKind.RESUME,
                session_id=target.session_id,
            )
        else:
            intent = PortableSessionIntent(
                kind=PortableSessionIntentKind.START,
                launch=self._launch,
            )
        try:
            snapshot = self._supervisor.handle_intent(intent)
        except (OSError, RuntimeError, ValueError) as error:
            self._write(str(error), PROBLEM)
            self._finish(_LAUNCH_FAILURE_EXIT_CODE)
            return
        # The supervisor decides which session actually took the work, which is
        # not always the id asked for: a launch can be folded into a session the
        # catalog already holds for this checkout.
        self._session_id = snapshot.session_id
        self._absorb(snapshot)

    def _drain(self) -> None:
        while True:
            event = self._supervisor.try_next_event()
            if event is None:
                return
            if event.snapshot.session_id != self._session_id:
                continue
            self._absorb(event.snapshot)

    def _absorb(self, snapshot: PortableSessionSnapshot) -> None:
        for line in self._stream.consume(snapshot):
            self._write(line.text, line.kind)
        self._snapshot = snapshot
        if (
            snapshot.status is PortableSessionStatus.WAITING_FOR_INPUT
            and snapshot.input_request is not None
        ):
            self._show_request(snapshot.input_request)
        self._refresh_hint()
        if snapshot.status.terminal:
            self._report_outcome(snapshot)
            self._finish(snapshot.result if snapshot.result is not None else 0)

    def _report_outcome(self, snapshot: PortableSessionSnapshot) -> None:
        if snapshot.status is PortableSessionStatus.COMPLETED:
            self._write("", OUTPUT)
            self._write("Workflow finished successfully.", NOTICE)
            return
        self._write("", OUTPUT)
        self._write(
            f"Workflow {snapshot.status.value.lower()} (exit code {snapshot.result}).",
            PROBLEM,
        )

    def _finish(self, code: int) -> None:
        if self._finished:
            return
        self._finished = True
        self.exit_code = code
        self.exit()

    # ---- operator input ----------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        typed = event.value
        event.input.value = ""
        snapshot = self._snapshot
        if self._finished or self._session_id is None or snapshot is None:
            return
        if (
            snapshot.status is PortableSessionStatus.WAITING_FOR_INPUT
            and snapshot.input_request is not None
        ):
            self._answer(snapshot.input_request, typed)
            return
        if snapshot.status in CONTINUABLE_STATUSES:
            self._continue_with(typed)
            return
        if typed.strip():
            # Guidance typed while the agent is mid-pass still lands on the
            # active issue; it is picked up when the next prompt is built.
            self._echo(typed.strip())
            self._record_guidance(typed.strip())

    def _answer(self, request: PortableSessionInputRequest, typed: str) -> None:
        assert self._session_id is not None
        try:
            value = resolve_answer(request, typed)
        except (UnresolvedAnswer, ValueError) as error:
            self._write(str(error), PROBLEM)
            return
        self._echo(typed.strip() or "(default)")
        self._dispatch(
            PortableSessionIntent(
                kind=PortableSessionIntentKind.PROVIDE_INPUT,
                session_id=self._session_id,
                value=value,
                request_id=request.request_id,
                request_generation=request.generation,
            )
        )

    def _continue_with(self, typed: str) -> None:
        assert self._session_id is not None
        text = typed.strip()
        if text:
            self._echo(text)
            if not self._record_guidance(text):
                return
        self._dispatch(
            PortableSessionIntent(
                kind=PortableSessionIntentKind.RESUME,
                session_id=self._session_id,
            )
        )

    def _dispatch(self, intent: PortableSessionIntent) -> None:
        """Send one intent, reporting refusals in the activity area instead of crashing."""
        try:
            self._supervisor.handle_intent(intent)
        except (OSError, RuntimeError, ValueError) as error:
            self._write(str(error), PROBLEM)

    def _record_guidance(self, text: str) -> bool:
        snapshot = self._snapshot
        issue = snapshot.progress.active_issue if snapshot is not None else None
        try:
            self._guidance.save(issue, text)
        except NoActiveIssue as error:
            self._write(
                f"{error} Press Enter on an empty line to continue without it.",
                PROBLEM,
            )
            return False
        except (OSError, ValueError) as error:
            self._write(f"Could not save that guidance: {error}", PROBLEM)
            return False
        self._write(
            f"Saved for issue {issue}. The agent reads it on its next pass.",
            NOTICE,
        )
        return True

    # ---- key actions --------------------------------------------------

    def action_pause(self) -> None:
        if not self._can_pause():
            return
        assert self._session_id is not None
        try:
            self._supervisor.pause_session(self._session_id)
        except (OSError, RuntimeError, ValueError) as error:
            self._write(str(error), PROBLEM)

    def action_interrupt(self) -> None:
        if self._can_pause():
            self.action_pause()
            return
        self._finish(_INTERRUPTED_EXIT_CODE)

    def _can_pause(self) -> bool:
        return (
            not self._finished
            and self._session_id is not None
            and self._snapshot is not None
            and self._snapshot.status in PAUSABLE_STATUSES
        )

    # ---- rendering -----------------------------------------------------

    def _show_request(self, request: PortableSessionInputRequest) -> None:
        identity = (request.request_id, request.generation)
        if identity == self._shown_request:
            return
        self._shown_request = identity
        self._write("", OUTPUT)
        for line in prompt_lines(request):
            self._write(line, NOTICE if line.startswith("  ") else OUTPUT)

    def _echo(self, text: str) -> None:
        self.query_one("#activity", RichLog).write(Text(f"> {text}", style="bold"))

    def _write(self, text: str, kind: str = OUTPUT) -> None:
        self.query_one("#activity", RichLog).write(Text(text, style=_STYLES[kind]))

    def _refresh_hint(self) -> None:
        snapshot = self._snapshot
        hint = "" if snapshot is None else _HINTS.get(snapshot.status, "")
        self.query_one("#hint", Static).update(Text(hint, style="dim"))
