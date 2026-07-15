from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Sequence, TextIO

from . import statusui
from .clipboard import capture_clipboard_image
from .codex_runner import resolve_codex_executable
from .lineeditor import LineEditor
from .statusui import Stage

TurnRunner = Callable[[Sequence[str], Path], "tuple[int, str]"]

UUID_PATTERN = re.compile(
    r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
    re.IGNORECASE,
)
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# Only these tokens are treated as slash commands. Anything else that starts
# with "/" (unknown /foo tokens, absolute POSIX paths like /home/x.png) falls
# through and is sent to Codex as a normal message.
KNOWN_COMMANDS = {"/help", "/status", "/options", "/paste", "/done", "/quit"}

HELP_TEXT = """Commands:
  Alt+V    attach a screenshot from the clipboard (use /paste if unavailable)
  /paste   attach a screenshot from the clipboard
  /options open agent/skill and development options
  /status  show the stage banner, artifacts, and selection summary
  /done    detect the PRD and issue pack now (or enter paths manually)
  /help    show this help
  /quit    abort planning (never required to continue)"""

CODEX_NOISE_PREFIXES = (
    "Reading additional input from stdin...",
    "OpenAI Codex v",
    "workdir:",
    "model:",
    "provider:",
    "approval:",
    "sandbox:",
    "reasoning effort:",
    "reasoning summaries:",
    "session id:",
)
CODEX_NOISE_LINES = {"--------", "user"}
WAITING_FRAMES = ("|", "/", "-", "\\")
WAITING_FRAME_SECONDS = 0.12
WAITING_STALL_SECONDS = 120.0
CODEX_EXIT_GRACE_SECONDS = 1.0
CODEX_TERMINATE_GRACE_SECONDS = 5.0
PLANNING_STAGE_STATUS = (
    "Pipeline: ANALYSIS active | PRD + ISSUES not detected | DEVELOPMENT waits"
)
PLANNING_SUBMISSION_STATUS = "Submitted to Codex; waiting for the planning response..."


class CodexTurnOutcome(Enum):
    COMPLETED = "turn.completed"
    FAILED = "turn.failed"


class WaitingIndicator:
    def __init__(
        self,
        stream: TextIO | None = None,
        frame_seconds: float = WAITING_FRAME_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        stalled_after_seconds: float = WAITING_STALL_SECONDS,
    ) -> None:
        self._stream = sys.stdout if stream is None else stream
        self._frame_seconds = frame_seconds
        self._clock = clock
        self._stalled_after_seconds = stalled_after_seconds
        isatty = getattr(self._stream, "isatty", None)
        self._enabled = bool(callable(isatty) and isatty())
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._activity_lock = threading.Lock()
        self._started_at = self._clock()
        self._last_activity_at: float | None = None
        self._rendered_width = 0

    def start(self) -> None:
        if not self._enabled or self._thread is not None:
            return
        self._stop_requested.clear()
        self._thread = threading.Thread(target=self._animate, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop_requested.set()
        self._thread.join()
        self._thread = None
        self._clear()

    def notify_activity(self) -> None:
        with self._activity_lock:
            self._last_activity_at = self._clock()

    def _animate(self) -> None:
        frame_index = 0
        while True:
            frame = WAITING_FRAMES[frame_index % len(WAITING_FRAMES)]
            status_line = self._status_line(frame)
            padding = " " * max(0, self._rendered_width - len(status_line))
            try:
                self._stream.write(f"\r{status_line}{padding}")
                self._stream.flush()
            except (OSError, ValueError):
                return
            self._rendered_width = max(self._rendered_width, len(status_line))
            if self._stop_requested.wait(self._frame_seconds):
                return
            frame_index += 1

    def _status_line(self, frame: str) -> str:
        now = self._clock()
        with self._activity_lock:
            last_activity_at = self._last_activity_at

        elapsed_seconds = max(0.0, now - self._started_at)
        inactivity_seconds = (
            elapsed_seconds
            if last_activity_at is None
            else max(0.0, now - last_activity_at)
        )
        elapsed = _format_duration(elapsed_seconds)
        inactivity = _format_duration(inactivity_seconds)

        if inactivity_seconds >= self._stalled_after_seconds:
            return (
                f"[analysis] POSSIBLY STALLED [{frame}] elapsed {elapsed} | "
                f"silent {inactivity} | Ctrl+C"
            )
        if last_activity_at is None:
            return (
                f"[analysis] Codex is working [{frame}] elapsed {elapsed} | "
                "waiting for first event"
            )
        return (
            f"[analysis] Codex is working [{frame}] elapsed {elapsed} | "
            f"last event {inactivity} ago"
        )

    def _clear(self) -> None:
        try:
            self._stream.write(f"\r{' ' * self._rendered_width}\r")
            self._stream.flush()
        except (OSError, ValueError):
            pass


def _format_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{remaining_seconds:02}"


@dataclass
class ChatConfig:
    codex: str
    repo_root: Path
    bundle_root: Path
    sandbox: str = "workspace-write"
    approval_policy: str = "never"


@dataclass
class ChatCallbacks:
    probe_artifacts: Callable[[], Any | None]
    manual_artifacts: Callable[[], Any | None]
    open_options: Callable[[], None]
    status_summary: Callable[[], str]


@dataclass
class ChatSession:
    config: ChatConfig
    session_id: str | None = None
    started: bool = False
    pending_images: list[Path] = field(default_factory=list)
    image_counter: int = 0
    consecutive_failures: int = 0

    def build_turn_command(self, message: str, first_prompt: str | None = None) -> list[str]:
        command: list[str] = [self.config.codex, "exec"]
        is_resume = first_prompt is None
        if is_resume:
            command.append("resume")
            if self.session_id:
                command.append(self.session_id)
            else:
                command.append("--last")
            command.append("--json")
            # `codex exec resume --help` (Codex CLI 0.143.0) does NOT accept
            # -C/--cd, --add-dir, or -s/--sandbox -- those are exec-only
            # options. The resumed session already carries the cwd and
            # sandbox chosen on the first turn, so they are dropped here
            # rather than passed (which would be a CLI error).
            command.extend(
                [
                    "-c",
                    f'approval_policy="{self.config.approval_policy}"',
                    "--skip-git-repo-check",
                ]
            )
        else:
            command.extend(
                [
                    "--json",
                    "-C",
                    str(self.config.repo_root),
                    "--add-dir",
                    str(self.config.bundle_root),
                    "-s",
                    self.config.sandbox,
                    "-c",
                    f'approval_policy="{self.config.approval_policy}"',
                    "--skip-git-repo-check",
                ]
            )
        for image in self.pending_images:
            command.extend(["-i", str(image)])
        command.append(first_prompt if first_prompt is not None else message)
        return command


def parse_session_id(output: str) -> str | None:
    for line in output.splitlines():
        session_id = _parse_session_id_from_json_line(line)
        if session_id:
            return session_id
        if "session" in line.lower():
            match = UUID_PATTERN.search(line)
            if match:
                return match.group(1)
    return None


def _parse_session_id_from_json_line(line: str) -> str | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    if payload.get("type") == "thread.started":
        thread_id = payload.get("thread_id")
        if isinstance(thread_id, str) and UUID_PATTERN.fullmatch(thread_id):
            return thread_id

    for key in ("session_id", "thread_id"):
        value = payload.get(key)
        if isinstance(value, str) and UUID_PATTERN.fullmatch(value):
            return value
    return None


def detect_image_paths(message: str) -> list[Path]:
    found: list[Path] = []
    for token in re.split(r"[\s\"']+", message):
        if not token or Path(token).suffix.lower() not in IMAGE_SUFFIXES:
            continue
        candidate = Path(token).expanduser()
        if candidate.is_file():
            found.append(candidate.resolve())
    return found


def run_streaming(command: Sequence[str], cwd: Path) -> tuple[int, str]:
    resolved_command = list(command)
    if resolved_command:
        resolved_command[0] = resolve_codex_executable(resolved_command[0])
    json_mode = "--json" in resolved_command
    try:
        process = subprocess.Popen(
            resolved_command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except FileNotFoundError:
        message = f"Codex executable not found: {command[0]}. Is Codex CLI installed and on PATH?"
        print(message, file=sys.stderr)
        return 127, message
    captured: list[str] = []
    assert process.stdout is not None
    waiting_indicator = WaitingIndicator()
    waiting_indicator.start()
    turn_outcome: CodexTurnOutcome | None = None
    try:
        for line in process.stdout:
            waiting_indicator.notify_activity()
            captured.append(line)
            rendered = _render_codex_stream_line(line) if json_mode else line
            if rendered:
                waiting_indicator.stop()
                sys.stdout.write(rendered)
                sys.stdout.flush()
                waiting_indicator.start()
            if json_mode:
                turn_outcome = _parse_codex_turn_outcome(line)
                if turn_outcome is not None:
                    break
        if turn_outcome is None:
            process.wait()
        else:
            _reap_process_after_terminal_event(process)
    except KeyboardInterrupt:
        # Do not let the child linger: terminate it, drain any buffered output,
        # then report the conventional 130 exit code with the partial output.
        process.terminate()
        try:
            remainder = process.stdout.read()
            if remainder:
                captured.append(remainder)
        except Exception:
            pass
        try:
            process.wait(timeout=5)
        except Exception:
            process.kill()
        return 130, "".join(captured)
    finally:
        waiting_indicator.stop()
    if turn_outcome is CodexTurnOutcome.COMPLETED:
        return 0, "".join(captured)
    if turn_outcome is CodexTurnOutcome.FAILED:
        returncode = process.returncode
        if isinstance(returncode, int) and returncode != 0:
            return returncode, "".join(captured)
        return 1, "".join(captured)
    return process.returncode, "".join(captured)


def _parse_codex_turn_outcome(line: str) -> CodexTurnOutcome | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    event_type = payload.get("type")
    if not isinstance(event_type, str):
        return None
    try:
        return CodexTurnOutcome(event_type)
    except ValueError:
        return None


def _reap_process_after_terminal_event(process: subprocess.Popen[str]) -> None:
    try:
        process.wait(timeout=CODEX_EXIT_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass

    process.terminate()
    try:
        process.wait(timeout=CODEX_TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass

    process.kill()
    try:
        process.wait(timeout=CODEX_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        # The protocol turn is already terminal. Never hold the Dev Loop handoff
        # indefinitely because an OS process refuses to be reaped.
        pass


def _render_codex_stream_line(line: str) -> str | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None if _is_codex_noise_line(line) else line
    if not isinstance(payload, dict):
        return None
    return _render_codex_json_event(payload)


def _is_codex_noise_line(line: str) -> bool:
    text = line.strip()
    if not text:
        return True
    if text in CODEX_NOISE_LINES:
        return True
    return any(text.startswith(prefix) for prefix in CODEX_NOISE_PREFIXES)


def _render_codex_json_event(payload: dict[str, Any]) -> str | None:
    event_type = str(payload.get("type", ""))
    if event_type == "item.completed":
        item = payload.get("item")
        if isinstance(item, dict):
            return _render_codex_json_item(item)

    if event_type in {"error", "turn.failed"}:
        message = _extract_text_payload(payload.get("message")) or _extract_text_payload(
            payload.get("error")
        )
        if message:
            return _line(message if message.startswith("ERROR:") else f"ERROR: {message}")

    if event_type in {"assistant_message", "agent_message"}:
        message = (
            _extract_text_payload(payload.get("message"))
            or _extract_text_payload(payload.get("content"))
            or _extract_text_payload(payload.get("text"))
        )
        if message:
            return _line(message)
    return None


def _render_codex_json_item(item: dict[str, Any]) -> str | None:
    item_type = str(item.get("type", ""))
    if item_type == "error":
        message = _extract_text_payload(item.get("message")) or _extract_text_payload(
            item.get("error")
        )
        if message:
            return _line(message if message.startswith("ERROR:") else f"ERROR: {message}")

    if item_type == "message" and item.get("role") == "assistant":
        message = _extract_text_payload(item.get("content"))
        if not message:
            message = _extract_text_payload(item.get("message")) or _extract_text_payload(
                item.get("text")
            )
        if message:
            return _line(message)

    if item_type in {"assistant_message", "agent_message"}:
        message = (
            _extract_text_payload(item.get("message"))
            or _extract_text_payload(item.get("content"))
            or _extract_text_payload(item.get("text"))
        )
        if message:
            return _line(message)
    return None


def _extract_text_payload(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_extract_text_payload(part) for part in value]
        return "".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("text", "message", "content", "delta", "error"):
            text = _extract_text_payload(value.get(key))
            if text:
                return text
    return ""


def _line(text: str) -> str:
    return text if text.endswith("\n") else f"{text}\n"


def run_planning_chat(
    *,
    config: ChatConfig,
    initial_prompt: str,
    callbacks: ChatCallbacks,
    collect_initial_message: bool = False,
    turn_runner: TurnRunner = run_streaming,
    editor: Any | None = None,
    capture_image: Callable[[Path], Path | None] = capture_clipboard_image,
) -> Any | None:
    session = ChatSession(config=config)
    image_dir = Path(tempfile.mkdtemp(prefix="devloop-images-"))

    def paste_hook() -> str | None:
        image = capture_image(image_dir)
        if image is None:
            print("\nNo image found on the clipboard.")
            return None
        session.pending_images.append(image)
        session.image_counter += 1
        return f"[image {session.image_counter} attached] "

    try:
        if editor is None:
            editor = LineEditor(on_paste_image=paste_hook)

        print(statusui.render_banner(Stage.ANALYSIS))
        print(
            "Describe the change. Type /status for the current phase, "
            "/help for commands; Alt+V pastes a screenshot."
        )

        if collect_initial_message:
            collected = _collect_initial_message(session, callbacks, editor, paste_hook)
            if collected.finished:
                return collected.result
            initial_prompt = append_initial_message(initial_prompt, collected.text)

        returncode, output = _run_turn(session, turn_runner, first_prompt=initial_prompt)
        if returncode == 0:
            session.started = True
        else:
            print(
                f"Codex could not start (exit {returncode}). "
                "Your next message will retry the planning session.",
                file=sys.stderr,
            )

        while True:
            artifacts = callbacks.probe_artifacts()
            if artifacts is not None:
                print("\nPRD and issue pack detected; continuing to development.")
                return artifacts

            # The banner stays visible: reprint it before every input prompt so the
            # current stage survives any amount of scrolled Codex output.
            print(statusui.render_banner(Stage.ANALYSIS))
            try:
                line = editor.read_line(statusui.stage_prompt(Stage.ANALYSIS))
            except EOFError:
                return None
            except KeyboardInterrupt:
                if _confirm_abort(editor):
                    return None
                continue

            text = line.strip()
            if not text:
                continue

            if text.split()[0].lower() in KNOWN_COMMANDS:
                handled, result, finished = _handle_command(
                    text, session, callbacks, editor, paste_hook
                )
                if finished:
                    return result
                if handled:
                    continue

            for image in detect_image_paths(text):
                if image not in session.pending_images:
                    session.pending_images.append(image)

            try:
                if not session.started:
                    returncode, output = _run_turn(session, turn_runner, first_prompt=initial_prompt)
                    if returncode == 0:
                        session.started = True
                        session.consecutive_failures = 0
                    else:
                        session.consecutive_failures += 1
                        continue
                    # The goal text the user just typed still needs to reach Codex.
                    returncode, output = _run_turn(session, turn_runner, message=text)
                elif session.consecutive_failures >= 1:
                    # The previous resume turn failed. Rather than resuming the same
                    # failing session again, start a fresh `codex exec` session with
                    # the complete planning contract plus the continuation message.
                    continuation = build_recovery_prompt(initial_prompt, text)
                    returncode, output = _run_fresh_turn(session, turn_runner, continuation)
                else:
                    returncode, output = _run_turn(session, turn_runner, message=text)
            except KeyboardInterrupt:
                print("\nCodex turn interrupted.")
                session.consecutive_failures += 1
                continue

            if returncode != 0:
                session.consecutive_failures += 1
                print(
                    f"Codex turn failed (exit {returncode}). Retry, rephrase, or /quit.",
                    file=sys.stderr,
                )
                continue
            session.consecutive_failures = 0
            session.pending_images.clear()
    finally:
        shutil.rmtree(image_dir, ignore_errors=True)


@dataclass(frozen=True)
class InitialMessage:
    text: str = ""
    result: Any | None = None
    finished: bool = False


def _collect_initial_message(
    session: ChatSession,
    callbacks: ChatCallbacks,
    editor: Any,
    paste_hook: Callable[[], str | None],
) -> InitialMessage:
    while True:
        try:
            line = editor.read_line(statusui.stage_prompt(Stage.ANALYSIS))
        except EOFError:
            return InitialMessage(finished=True)
        except KeyboardInterrupt:
            if _confirm_abort(editor):
                return InitialMessage(finished=True)
            print(statusui.render_banner(Stage.ANALYSIS))
            continue

        text = line.strip()
        if not text:
            continue

        if text.split()[0].lower() in KNOWN_COMMANDS:
            handled, result, finished = _handle_command(text, session, callbacks, editor, paste_hook)
            if finished:
                return InitialMessage(result=result, finished=True)
            if handled:
                print(statusui.render_banner(Stage.ANALYSIS))
                continue

        for image in detect_image_paths(text):
            if image not in session.pending_images:
                session.pending_images.append(image)

        return InitialMessage(text=text)


def append_initial_message(initial_prompt: str, message: str) -> str:
    return f"{initial_prompt.rstrip()}\n\nInitial user goal:\n{message}"


def build_recovery_prompt(initial_prompt: str, message: str) -> str:
    return (
        f"{initial_prompt.rstrip()}\n\n"
        "Recovery note:\n"
        "Continuing an interrupted Dev Loop planning session because the previous "
        "Codex session could not be resumed. The complete planning contract above "
        "still applies: remain in analysis, create the PRD and issue pack, and do "
        "not start implementation.\n\n"
        f"Current user message:\n{message}"
    )


def _run_turn(
    session: ChatSession,
    turn_runner: TurnRunner,
    *,
    message: str = "",
    first_prompt: str | None = None,
) -> tuple[int, str]:
    command = session.build_turn_command(message, first_prompt=first_prompt)
    _print_planning_submission()
    returncode, output = turn_runner(command, session.config.repo_root)
    if returncode == 0 and session.session_id is None:
        session.session_id = parse_session_id(output)
    return returncode, output


def _run_fresh_turn(
    session: ChatSession,
    turn_runner: TurnRunner,
    prompt: str,
) -> tuple[int, str]:
    """Start a brand-new `codex exec` session (never resume) with ``prompt``.

    Used to recover after a resume turn fails: the old (failing) session id is
    replaced with the one parsed from this fresh session so subsequent turns
    resume the new session.
    """
    command = session.build_turn_command("", first_prompt=prompt)
    _print_planning_submission()
    returncode, output = turn_runner(command, session.config.repo_root)
    if returncode == 0:
        session.session_id = parse_session_id(output)
        session.started = True
    return returncode, output


def _print_planning_submission() -> None:
    print(f"\n{PLANNING_STAGE_STATUS}")
    print(PLANNING_SUBMISSION_STATUS)


def _handle_command(
    text: str,
    session: ChatSession,
    callbacks: ChatCallbacks,
    editor: Any,
    paste_hook: Callable[[], str | None],
) -> tuple[bool, Any | None, bool]:
    """Returns (handled, result, finished)."""
    command = text.split()[0].lower()
    if command == "/help":
        print(HELP_TEXT)
        return True, None, False
    if command == "/status":
        print(statusui.render_banner(Stage.ANALYSIS))
        print(PLANNING_STAGE_STATUS)
        print(callbacks.status_summary())
        # Detection exits the chat loop the moment a probe succeeds, so mid-chat
        # the probe is always None here; report that honestly.
        if callbacks.probe_artifacts() is None:
            print("Artifacts: none detected yet")
        if session.pending_images:
            print(f"Pending images: {len(session.pending_images)}")
        return True, None, False
    if command == "/options":
        callbacks.open_options()
        return True, None, False
    if command == "/paste":
        token = paste_hook()
        if token:
            print(f"Attached. Include it in your next message: {token.strip()}")
        return True, None, False
    if command == "/done":
        artifacts = callbacks.probe_artifacts()
        if artifacts is None:
            artifacts = callbacks.manual_artifacts()
        if artifacts is None:
            print("No artifacts selected; continuing the planning chat.")
            return True, None, False
        return True, artifacts, True
    if command == "/quit":
        if _confirm_abort(editor):
            return True, None, True
        return True, None, False
    # Unreachable: the loop only routes KNOWN_COMMANDS here. Fall through as a
    # no-op rather than emitting a misleading "unknown command" hint.
    return True, None, False


def _confirm_abort(editor: Any) -> bool:
    try:
        answer = editor.read_line("Abort planning? [y/N]: ")
    except (EOFError, KeyboardInterrupt):
        return True
    return answer.strip().lower() in {"y", "yes"}
