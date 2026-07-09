from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from . import statusui
from .clipboard import capture_clipboard_image
from .lineeditor import LineEditor
from .statusui import Stage

TurnRunner = Callable[[Sequence[str], Path], "tuple[int, str]"]

UUID_PATTERN = re.compile(
    r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
    re.IGNORECASE,
)
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

HELP_TEXT = """Commands:
  Alt+V    attach a screenshot from the clipboard (use /paste if unavailable)
  /paste   attach a screenshot from the clipboard
  /options open agent/skill and development options
  /status  show the stage banner, artifacts, and selection summary
  /done    detect the PRD and issue pack now (or enter paths manually)
  /help    show this help
  /quit    abort planning (never required to continue)"""


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

    def build_turn_command(self, message: str, first_prompt: str | None = None) -> list[str]:
        command: list[str] = [self.config.codex, "exec"]
        is_resume = first_prompt is None
        if is_resume:
            command.append("resume")
            if self.session_id:
                command.append(self.session_id)
            else:
                command.append("--last")
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
        if "session" in line.lower():
            match = UUID_PATTERN.search(line)
            if match:
                return match.group(1)
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
    try:
        process = subprocess.Popen(
            list(command),
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
    for line in process.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        captured.append(line)
    process.wait()
    return process.returncode, "".join(captured)


def run_planning_chat(
    *,
    config: ChatConfig,
    initial_prompt: str,
    callbacks: ChatCallbacks,
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
        print("Describe the change. Type /help for commands; Alt+V pastes a screenshot.")

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

            if text.startswith("/"):
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

            if not session.started:
                returncode, output = _run_turn(session, turn_runner, first_prompt=initial_prompt)
                if returncode == 0:
                    session.started = True
                else:
                    continue
                # The goal text the user just typed still needs to reach Codex.
                returncode, output = _run_turn(session, turn_runner, message=text)
            else:
                returncode, output = _run_turn(session, turn_runner, message=text)

            if returncode != 0:
                print(
                    f"Codex turn failed (exit {returncode}). Retry, rephrase, or /quit.",
                    file=sys.stderr,
                )
                continue
            session.pending_images.clear()
    finally:
        shutil.rmtree(image_dir, ignore_errors=True)


def _run_turn(
    session: ChatSession,
    turn_runner: TurnRunner,
    *,
    message: str = "",
    first_prompt: str | None = None,
) -> tuple[int, str]:
    command = session.build_turn_command(message, first_prompt=first_prompt)
    returncode, output = turn_runner(command, session.config.repo_root)
    if returncode == 0 and session.session_id is None:
        session.session_id = parse_session_id(output)
    return returncode, output


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
        print(callbacks.status_summary())
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
    print(f"Unknown command: {command}. Type /help for commands.")
    return True, None, False


def _confirm_abort(editor: Any) -> bool:
    try:
        answer = editor.read_line("Abort planning? [y/N]: ")
    except (EOFError, KeyboardInterrupt):
        return True
    return answer.strip().lower() in {"y", "yes"}
