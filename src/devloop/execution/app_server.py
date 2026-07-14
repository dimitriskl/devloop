from __future__ import annotations

import math
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import BinaryIO, cast

from devloop.execution.protocol import (
    MAX_PROTOCOL_LINE_BYTES,
    JsonLineCodec,
    ProtocolError,
    RequestId,
    RpcNotification,
    RpcRequestTracker,
    RpcResponse,
    RpcServerRequest,
    RpcServerResponse,
)
from devloop.execution.windows_process_tree import (
    WindowsProcessTree,
    WindowsProcessTreeError,
    create_windows_process_tree,
)
from devloop.version import VERSION

_SAFE_ERROR_CODE = re.compile(r"[A-Za-z][A-Za-z0-9]{0,63}\Z")
_SUPPORTED_ACCOUNT_TYPES = frozenset({"apiKey", "chatgpt", "amazonBedrock"})
_SUPPORTED_BEDROCK_CREDENTIAL_SOURCES = frozenset({"codexManaged", "awsManaged"})
_MAX_OPERATION_TIMEOUT_SECONDS = 10.0
_PROCESS_STOP_GRACE_SECONDS = 1.0
_POSIX_TERMINATE_SIGNAL = int(getattr(signal, "SIGTERM", 15))
_POSIX_KILL_SIGNAL = int(getattr(signal, "SIGKILL", 9))
_CLIENT_NAME = "devloop-codexcli"
_CLIENT_TITLE = "Dev Loop"
_THREAD_START_METHOD = "thread/start"
_THREAD_RESUME_METHOD = "thread/resume"
_THREAD_READ_METHOD = "thread/read"
_TURN_START_METHOD = "turn/start"
_TURN_INTERRUPT_METHOD = "turn/interrupt"
_TURN_COMPLETED_METHOD = "turn/completed"
_AGENT_MESSAGE_DELTA_METHOD = "item/agentMessage/delta"
_ITEM_STARTED_METHOD = "item/started"
_ITEM_COMPLETED_METHOD = "item/completed"
_MAX_TURN_TIMEOUT_SECONDS = 3600.0
_INTERRUPT_POLL_SECONDS = 0.1
_TURN_INTERRUPT_COMPLETION_GRACE_SECONDS = 2.0
_TRANSIENT_TURN_FAILURE_CODES = frozenset(
    {
        "httpConnectionFailed",
        "internalServerError",
        "responseStreamConnectionFailed",
        "responseStreamDisconnected",
        "responseTooManyFailedAttempts",
        "serverOverloaded",
    }
)
_TRANSIENT_TURN_FAILURE_MESSAGES = (
    "stream disconnected before completion",
    "failed to connect to the response stream",
)
_COOPERATIVE_CANCELLATION: ContextVar[Callable[[], bool] | None] = ContextVar(
    "devloop_app_server_cancellation",
    default=None,
)


class _StreamClosed:
    pass


class _StreamFailed:
    pass


class _LineTooLarge:
    pass


_STREAM_CLOSED = _StreamClosed()
_STREAM_FAILED = _StreamFailed()
_LINE_TOO_LARGE = _LineTooLarge()
_ReaderItem = bytes | _StreamClosed | _StreamFailed | _LineTooLarge


class AppServerError(RuntimeError):
    pass


class AppServerTransientError(AppServerError):
    pass


@contextmanager
def cooperative_cancellation(
    cancellation_requested: Callable[[], bool],
) -> Iterator[None]:
    """Make worker cancellation visible to App Server waits in this thread."""

    token = _COOPERATIVE_CANCELLATION.set(cancellation_requested)
    try:
        yield
    finally:
        _COOPERATIVE_CANCELLATION.reset(token)


@dataclass(frozen=True)
class AppServerHandshake:
    platform_family: str
    platform_os: str

    @classmethod
    def from_result(cls, result: Mapping[str, object]) -> AppServerHandshake:
        platform_family = result.get("platformFamily")
        platform_os = result.get("platformOs")
        if not isinstance(platform_family, str) or not isinstance(platform_os, str):
            raise AppServerError("App Server initialize result is missing platform information.")
        return cls(platform_family=platform_family, platform_os=platform_os)


@dataclass(frozen=True)
class AuthenticationReadiness:
    ready: bool
    requires_openai_auth: bool
    mode: str | None

    @classmethod
    def from_result(cls, result: Mapping[str, object]) -> AuthenticationReadiness:
        requires_openai_auth = result.get("requiresOpenaiAuth")
        account = result.get("account")
        if not isinstance(requires_openai_auth, bool):
            raise AppServerError("App Server account result is missing authentication status.")
        if account is not None and not isinstance(account, dict):
            raise AppServerError("App Server account result has an invalid account object.")

        mode: str | None = None
        if isinstance(account, dict):
            account_data = cast(dict[str, object], account)
            mode = _validated_account_mode(account_data)

        return cls(
            ready=not requires_openai_auth or account is not None,
            requires_openai_auth=requires_openai_auth,
            mode=mode,
        )


def _validated_account_mode(account: Mapping[str, object]) -> str:
    candidate = account.get("type")
    if not isinstance(candidate, str) or candidate not in _SUPPORTED_ACCOUNT_TYPES:
        raise AppServerError(
            "App Server account result has an invalid or unsupported account type."
        )
    if candidate == "chatgpt":
        email = account.get("email")
        plan_type = account.get("planType")
        if "email" not in account or (email is not None and not isinstance(email, str)):
            raise AppServerError("App Server account result has invalid ChatGPT details.")
        if not isinstance(plan_type, str) or not plan_type:
            raise AppServerError("App Server account result has invalid ChatGPT details.")
    if candidate == "amazonBedrock" and "credentialSource" in account:
        credential_source = account.get("credentialSource")
        if credential_source not in _SUPPORTED_BEDROCK_CREDENTIAL_SOURCES:
            raise AppServerError("App Server account result has invalid Bedrock details.")
    return candidate


@dataclass(frozen=True)
class AppServerStatus:
    """Sanitized App Server compatibility and authentication status."""

    handshake: AppServerHandshake
    authentication: AuthenticationReadiness


class AppServerTurnStatus(str, Enum):
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    IN_PROGRESS = "inProgress"


class AppServerSandboxMode(str, Enum):
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"


class AppServerApprovalPolicy(str, Enum):
    NEVER = "never"
    ON_REQUEST = "on-request"


class AppServerApprovalsReviewer(str, Enum):
    USER = "user"


class AppServerReasoningEffort(str, Enum):
    XHIGH = "xhigh"
    ULTRA = "ultra"


class AppServerPermissionProfile(str, Enum):
    READ_ONLY = ":read-only"
    WORKSPACE = ":workspace"


class AppServerApprovalKind(str, Enum):
    COMMAND = "COMMAND"
    FILE_CHANGE = "FILE_CHANGE"
    PERMISSIONS = "PERMISSIONS"
    TOOL_INPUT = "TOOL_INPUT"
    UNKNOWN = "UNKNOWN"


class AppServerRequestMethod(str, Enum):
    COMMAND_APPROVAL = "item/commandExecution/requestApproval"
    FILE_CHANGE_APPROVAL = "item/fileChange/requestApproval"
    PERMISSIONS_APPROVAL = "item/permissions/requestApproval"
    TOOL_INPUT = "item/tool/requestUserInput"


class AppServerCommandApprovalDecision(str, Enum):
    ACCEPT = "accept"


@dataclass(frozen=True)
class AppServerApprovalRequest:
    request_id: RequestId
    kind: AppServerApprovalKind
    method: str
    action: str = "Review a Codex request"
    target: str | None = None
    reason: str | None = None
    supported_decisions: tuple[str, ...] = ()
    thread_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None


class AppServerApprovalRequired(AppServerError):
    def __init__(self, request: AppServerApprovalRequest) -> None:
        super().__init__(f"Codex App Server requires user approval for {request.kind.value}.")
        self.request = request


@dataclass(frozen=True)
class AppServerThread:
    thread_id: str


@dataclass(frozen=True)
class AppServerTurn:
    turn_id: str


@dataclass(frozen=True)
class AppServerTurnResult:
    thread_id: str
    turn_id: str
    status: AppServerTurnStatus
    message: str
    completed_item_ids: tuple[str, ...]
    failure_code: str | None = None
    failure_message: str | None = None


def is_transient_turn_failure(result: AppServerTurnResult) -> bool:
    """Return whether a terminal turn failure is safe for bounded retry."""

    if result.status is not AppServerTurnStatus.FAILED:
        return False
    if result.failure_code in _TRANSIENT_TURN_FAILURE_CODES:
        return True
    if result.failure_message is None:
        return False
    normalized = " ".join(result.failure_message.casefold().split())
    return any(marker in normalized for marker in _TRANSIENT_TURN_FAILURE_MESSAGES)


class AppServerClient:
    """Synchronous JSONL client for one real Codex App Server process."""

    def __init__(
        self,
        executable: str,
        timeout_seconds: float = 10.0,
        *,
        experimental_api: bool = False,
        process_cwd: Path | None = None,
        server_request_handler: (
            Callable[[RpcServerRequest], Mapping[str, object] | None] | None
        ) = None,
        approval_handler: Callable[[AppServerApprovalRequest], str | None] | None = None,
        environment_overrides: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(executable, str) or not executable.strip():
            raise ValueError("A Codex executable is required.")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > _MAX_OPERATION_TIMEOUT_SECONDS
        ):
            raise ValueError("App Server timeout must be greater than zero and at most 10 seconds.")

        self._executable = executable
        self._timeout_seconds = float(timeout_seconds)
        self._experimental_api = experimental_api
        self._process_cwd = None if process_cwd is None else process_cwd.resolve()
        self._server_request_handler = server_request_handler
        self._approval_handler = approval_handler
        self._environment_overrides = dict(environment_overrides or {})
        self._process: subprocess.Popen[bytes] | None = None
        self._process_group_id: int | None = None
        self._windows_process_tree: WindowsProcessTree | None = None
        self._reader: threading.Thread | None = None
        self._reader_items: queue.Queue[_ReaderItem] = queue.Queue()
        self._notifications: queue.Queue[RpcNotification] = queue.Queue()
        self._tracker = RpcRequestTracker()
        self._initialized = False

    def __enter__(self) -> AppServerClient:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        self.close()

    def start(self) -> None:
        """Start `codex app-server --listen stdio://` if it is not already running."""

        process = self._process
        if process is not None:
            if process.poll() is None:
                return
            raise AppServerTransientError("Codex App Server stopped unexpectedly.")

        command = _executable_command(self._executable)
        start_new_session = not sys.platform.startswith("win")
        windows_process_tree = (
            _create_windows_process_tree() if sys.platform.startswith("win") else None
        )
        creation_flags = (
            windows_process_tree.creation_flags()
            if windows_process_tree is not None
            else 0
        )
        try:
            environment = os.environ.copy()
            environment.update(self._environment_overrides)
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                cwd=self._process_cwd,
                env=environment,
                start_new_session=start_new_session,
                creationflags=creation_flags,
            )
        except OSError:
            if windows_process_tree is not None:
                windows_process_tree.close()
            raise AppServerTransientError("Unable to start Codex App Server.") from None
        except ValueError:
            if windows_process_tree is not None:
                windows_process_tree.close()
            raise AppServerError("Codex App Server start configuration is invalid.") from None

        if windows_process_tree is not None:
            try:
                windows_process_tree.assign_and_resume(process)
            except WindowsProcessTreeError:
                windows_process_tree.close()
                self._stop_process(process, None, None)
                raise AppServerTransientError(
                    "Unable to establish App Server process-tree ownership."
                ) from None

        process_group_id = (
            self._dedicated_posix_process_group_id(process) if start_new_session else None
        )
        if process.stdin is None or process.stdout is None:
            self._stop_process(process, process_group_id, windows_process_tree)
            raise AppServerTransientError("Unable to open Codex App Server stdio.")

        self._process = process
        self._process_group_id = process_group_id
        self._windows_process_tree = windows_process_tree
        self._reader_items = queue.Queue()
        self._notifications = queue.Queue()
        self._tracker = RpcRequestTracker()
        self._initialized = False
        self._reader = threading.Thread(
            target=self._read_stdout,
            args=(process.stdout, self._reader_items),
            name="codex-app-server-stdout",
            daemon=True,
        )
        self._reader.start()

    def initialize(self) -> AppServerHandshake:
        """Initialize the server and acknowledge readiness with `initialized`."""

        if self._initialized:
            raise AppServerError("Codex App Server is already initialized.")
        result = self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": _CLIENT_NAME,
                    "title": _CLIENT_TITLE,
                    "version": VERSION,
                },
                "capabilities": {"experimentalApi": self._experimental_api},
            },
        )
        handshake = AppServerHandshake.from_result(_result_object(result, "initialize"))
        self._send_notification("initialized", {})
        self._initialized = True
        return handshake

    def read_account(self) -> AuthenticationReadiness:
        """Read only sanitized authentication readiness after initialization."""

        if not self._initialized:
            raise AppServerError(
                "Codex App Server must be initialized before reading account status."
            )
        result = self._request("account/read", {"refreshToken": False})
        return AuthenticationReadiness.from_result(_result_object(result, "account/read"))

    def probe(self) -> AppServerStatus:
        """Run initialize and account/read on the current client process."""

        self.start()
        return AppServerStatus(
            handshake=self.initialize(),
            authentication=self.read_account(),
        )

    def start_thread(
        self,
        cwd: Path,
        *,
        model: str | None = None,
        reasoning_effort: AppServerReasoningEffort | None = None,
        developer_instructions: str | None = None,
        sandbox: AppServerSandboxMode = AppServerSandboxMode.READ_ONLY,
        approval_policy: AppServerApprovalPolicy = AppServerApprovalPolicy.NEVER,
        approvals_reviewer: AppServerApprovalsReviewer | None = None,
        permission_profile: AppServerPermissionProfile | None = None,
    ) -> AppServerThread:
        self._require_initialized()
        params: dict[str, object] = {
            "cwd": str(cwd.resolve()),
            "approvalPolicy": approval_policy.value,
            "ephemeral": False,
        }
        if permission_profile is None:
            params["sandbox"] = sandbox.value
        else:
            params["permissions"] = permission_profile.value
        if approvals_reviewer is not None:
            params["approvalsReviewer"] = approvals_reviewer.value
        if model is not None:
            params["model"] = model
        if reasoning_effort is not None:
            params["config"] = {"model_reasoning_effort": reasoning_effort.value}
        if developer_instructions is not None:
            params["developerInstructions"] = developer_instructions
        result = _result_object(self._request(_THREAD_START_METHOD, params), _THREAD_START_METHOD)
        return AppServerThread(_thread_id(result, _THREAD_START_METHOD))

    def resume_thread(self, thread_id: str, cwd: Path) -> AppServerThread:
        self._require_initialized()
        result = _result_object(
            self._request(
                _THREAD_RESUME_METHOD,
                {"threadId": thread_id, "cwd": str(cwd.resolve())},
            ),
            _THREAD_RESUME_METHOD,
        )
        resumed_id = _thread_id(result, _THREAD_RESUME_METHOD)
        if resumed_id != thread_id:
            raise AppServerError("Codex App Server resumed a different thread.")
        return AppServerThread(resumed_id)

    def read_thread_turn_status(
        self,
        thread_id: str,
        turn_id: str,
    ) -> AppServerTurnStatus | None:
        """Read the exact stored turn without loading or resuming its thread."""

        self._require_initialized()
        result = _result_object(
            self._request(
                _THREAD_READ_METHOD,
                {"threadId": thread_id, "includeTurns": True},
            ),
            _THREAD_READ_METHOD,
        )
        if _thread_id(result, _THREAD_READ_METHOD) != thread_id:
            raise AppServerError("Codex App Server read a different thread.")
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise AppServerError("Codex App Server thread/read result is missing the thread.")
        turns = cast(dict[str, object], thread).get("turns")
        if not isinstance(turns, list):
            raise AppServerError("Codex App Server thread/read result is missing turns.")
        for turn_value in turns:
            if not isinstance(turn_value, dict):
                continue
            turn = cast(dict[str, object], turn_value)
            if turn.get("id") != turn_id:
                continue
            try:
                return AppServerTurnStatus(turn.get("status"))
            except (TypeError, ValueError):
                raise AppServerError(
                    "Codex App Server returned an unknown stored turn status."
                ) from None
        return None

    def resume_thread_with_turn(
        self,
        thread_id: str,
        cwd: Path,
        turn_id: str,
    ) -> tuple[AppServerThread, AppServerTurnResult | None]:
        self._require_initialized()
        result = _result_object(
            self._request(
                _THREAD_RESUME_METHOD,
                {"threadId": thread_id, "cwd": str(cwd.resolve())},
            ),
            _THREAD_RESUME_METHOD,
        )
        resumed_id = _thread_id(result, _THREAD_RESUME_METHOD)
        if resumed_id != thread_id:
            raise AppServerError("Codex App Server resumed a different thread.")
        thread_value = result.get("thread")
        if not isinstance(thread_value, dict):
            return AppServerThread(resumed_id), None
        turns = cast(dict[str, object], thread_value).get("turns")
        if not isinstance(turns, list):
            return AppServerThread(resumed_id), None
        for turn_value in turns:
            if not isinstance(turn_value, dict):
                continue
            turn = cast(dict[str, object], turn_value)
            if turn.get("id") != turn_id:
                continue
            notification = RpcNotification(
                _TURN_COMPLETED_METHOD,
                {"threadId": thread_id, "turn": turn},
            )
            recovered = _consume_turn_notification(
                notification,
                thread_id=thread_id,
                turn_id=turn_id,
                deltas=[],
                on_agent_delta=None,
                on_item_started=None,
                on_item_completed=None,
            )
            return AppServerThread(resumed_id), recovered
        return AppServerThread(resumed_id), None

    def start_turn(
        self,
        thread_id: str,
        text: str,
        *,
        output_schema: Mapping[str, object] | None = None,
    ) -> AppServerTurn:
        self._require_initialized()
        if not text.strip():
            raise ValueError("A nonempty turn message is required.")
        params: dict[str, object] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text}],
        }
        if output_schema is not None:
            params["outputSchema"] = dict(output_schema)
        result = _result_object(self._request(_TURN_START_METHOD, params), _TURN_START_METHOD)
        turn = result.get("turn")
        if not isinstance(turn, dict):
            raise AppServerError("Codex App Server turn/start result is missing the turn.")
        turn_data = cast(dict[str, object], turn)
        turn_id = turn_data.get("id")
        if not isinstance(turn_id, str) or not turn_id:
            raise AppServerError("Codex App Server turn/start result is missing the turn ID.")
        return AppServerTurn(turn_id)

    def wait_for_turn(
        self,
        thread_id: str,
        turn_id: str,
        *,
        timeout_seconds: float,
        on_agent_delta: Callable[[str], None] | None = None,
        on_item_started: Callable[[str], None] | None = None,
        on_item_completed: Callable[[str], None] | None = None,
        interrupt_requested: Callable[[], bool] | None = None,
    ) -> AppServerTurnResult:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > _MAX_TURN_TIMEOUT_SECONDS
        ):
            raise ValueError("Turn timeout must be greater than zero and at most 3600 seconds.")
        self._require_initialized()
        scoped_cancellation = _COOPERATIVE_CANCELLATION.get()

        def cancellation_requested() -> bool:
            return bool(
                (interrupt_requested is not None and interrupt_requested())
                or (scoped_cancellation is not None and scoped_cancellation())
            )

        can_cancel = interrupt_requested is not None or scoped_cancellation is not None
        deadline = time.monotonic() + timeout_seconds
        deltas: list[str] = []
        interrupt_sent = False
        interrupt_deadline: float | None = None
        while True:
            notification = self._take_notification()
            if notification is not None:
                completed = _consume_turn_notification(
                    notification,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    deltas=deltas,
                    on_agent_delta=on_agent_delta,
                    on_item_started=on_item_started,
                    on_item_completed=on_item_completed,
                )
                if completed is not None:
                    return completed
                continue
            if interrupt_deadline is not None and time.monotonic() >= interrupt_deadline:
                self.close()
                return AppServerTurnResult(
                    thread_id,
                    turn_id,
                    AppServerTurnStatus.INTERRUPTED,
                    "".join(deltas),
                    (),
                    failure_code="interruptionUnconfirmed",
                    failure_message=(
                        "Codex App Server did not confirm turn completion after interruption; "
                        "the outcome is unknown."
                    ),
                )
            if (
                not interrupt_sent
                and can_cancel
                and cancellation_requested()
            ):
                try:
                    self.interrupt_turn(thread_id, turn_id)
                except AppServerError:
                    # The turn may have become terminal before its completion
                    # notification reached this client. Keep consuming the exact
                    # turn instead of losing its persisted terminal state.
                    pass
                interrupt_sent = True
                interrupt_deadline = min(
                    deadline,
                    time.monotonic() + _TURN_INTERRUPT_COMPLETION_GRACE_SECONDS,
                )
                continue
            remaining = deadline - time.monotonic()
            if interrupt_deadline is not None:
                remaining = min(remaining, interrupt_deadline - time.monotonic())
            if remaining <= 0:
                if interrupt_deadline is not None:
                    continue
                raise AppServerTransientError(
                    "Codex App Server timed out waiting for turn completion."
                )
            try:
                wait_seconds = (
                    min(remaining, _INTERRUPT_POLL_SECONDS)
                    if can_cancel and not interrupt_sent
                    else remaining
                )
                item = self._reader_items.get(timeout=wait_seconds)
            except queue.Empty:
                if can_cancel and not interrupt_sent:
                    continue
                if interrupt_deadline is not None:
                    continue
                raise AppServerTransientError(
                    "Codex App Server timed out waiting for turn completion."
                ) from None
            self._route_reader_item(item)

    def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        self._require_initialized()
        if not thread_id or not turn_id:
            raise ValueError("Thread and turn IDs are required for interruption.")
        _result_object(
            self._request(
                _TURN_INTERRUPT_METHOD,
                {"threadId": thread_id, "turnId": turn_id},
            ),
            _TURN_INTERRUPT_METHOD,
        )

    def run_turn(
        self,
        thread_id: str,
        text: str,
        *,
        output_schema: Mapping[str, object] | None = None,
        timeout_seconds: float = 600.0,
        on_agent_delta: Callable[[str], None] | None = None,
        on_item_started: Callable[[str], None] | None = None,
        on_item_completed: Callable[[str], None] | None = None,
    ) -> AppServerTurnResult:
        turn = self.start_turn(thread_id, text, output_schema=output_schema)
        return self.wait_for_turn(
            thread_id,
            turn.turn_id,
            timeout_seconds=timeout_seconds,
            on_agent_delta=on_agent_delta,
            on_item_started=on_item_started,
            on_item_completed=on_item_completed,
        )

    def close(self) -> None:
        """Terminate the child, escalating to kill if it does not stop promptly."""

        process = self._process
        process_group_id = self._process_group_id
        windows_process_tree = self._windows_process_tree
        reader = self._reader
        self._process = None
        self._process_group_id = None
        self._windows_process_tree = None
        self._reader = None
        self._initialized = False
        self._tracker = RpcRequestTracker()
        self._notifications = queue.Queue()
        if process is None:
            return

        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        stop_error: AppServerError | None = None
        try:
            self._stop_process(process, process_group_id, windows_process_tree)
        except AppServerError as error:
            stop_error = error
        finally:
            if reader is not None:
                reader.join(timeout=_PROCESS_STOP_GRACE_SECONDS)
            if process.stdout is not None and (reader is None or not reader.is_alive()):
                try:
                    process.stdout.close()
                except OSError:
                    pass
        if stop_error is not None:
            raise stop_error

    def _request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> object | None:
        request = self._tracker.create_request(method, params)
        self._write_message(JsonLineCodec.encode(request))
        deadline = time.monotonic() + (
            self._timeout_seconds if timeout_seconds is None else timeout_seconds
        )

        while True:
            response = self._tracker.take_response(request.request_id)
            if response is not None:
                return self._response_result(response, method)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerTransientError(f"Codex App Server timed out during {method}.")
            try:
                item = self._reader_items.get(timeout=remaining)
            except queue.Empty:
                raise AppServerTransientError(
                    f"Codex App Server timed out during {method}."
                ) from None
            self._route_reader_item(item)

    def _send_notification(self, method: str, params: Mapping[str, object]) -> None:
        self._write_message(JsonLineCodec.encode(RpcNotification(method=method, params=params)))

    def _write_message(self, encoded: str) -> None:
        process = self._require_running_process()
        if process.stdin is None:
            raise AppServerTransientError("Codex App Server stdio is unavailable.")
        try:
            process.stdin.write(encoded.encode("utf-8"))
            process.stdin.flush()
        except (BrokenPipeError, OSError, UnicodeError):
            raise AppServerTransientError(
                "Unable to communicate with Codex App Server."
            ) from None

    def _route_reader_item(self, item: _ReaderItem) -> None:
        if item is _STREAM_CLOSED:
            raise AppServerTransientError("Codex App Server stopped before responding.")
        if item is _STREAM_FAILED:
            raise AppServerTransientError("Unable to read Codex App Server output.")
        if item is _LINE_TOO_LARGE:
            raise AppServerError("Codex App Server returned an oversized protocol message.")
        if not isinstance(item, bytes):
            raise AppServerError("Codex App Server returned an unknown stream state.")

        try:
            message = JsonLineCodec.decode(item.decode("utf-8", errors="strict"))
            if isinstance(message, RpcServerRequest):
                if self._server_request_handler is not None:
                    result = self._server_request_handler(message)
                    if result is not None:
                        self._write_message(
                            JsonLineCodec.encode(RpcServerResponse(message.request_id, result))
                        )
                        return
                approval = _approval_request(message)
                if self._approval_handler is not None:
                    decision = self._approval_handler(approval)
                    if decision is not None:
                        if decision not in approval.supported_decisions:
                            raise AppServerError(
                                "Approval handler selected a backend-unsupported decision."
                            )
                        self._write_message(
                            JsonLineCodec.encode(
                                RpcServerResponse(
                                    message.request_id,
                                    {"decision": decision},
                                )
                            )
                        )
                        return
                raise AppServerApprovalRequired(approval)
            if isinstance(message, RpcNotification):
                self._notifications.put(message)
            self._tracker.route(message)
        except UnicodeError:
            raise AppServerError("Codex App Server returned invalid UTF-8.") from None
        except ProtocolError:
            raise AppServerError("Codex App Server returned an invalid protocol message.") from None

    def _require_running_process(self) -> subprocess.Popen[bytes]:
        process = self._process
        if process is None:
            raise AppServerTransientError("Codex App Server is not running.")
        if process.poll() is not None:
            raise AppServerTransientError("Codex App Server stopped unexpectedly.")
        return process

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise AppServerError("Codex App Server must be initialized before starting work.")

    def _take_notification(self) -> RpcNotification | None:
        try:
            return self._notifications.get_nowait()
        except queue.Empty:
            return None

    @staticmethod
    def _response_result(response: RpcResponse, method: str) -> object | None:
        if response.error is not None:
            raise AppServerError(
                f"Codex App Server rejected {method} (error code {response.error.code})."
            )
        return response.result

    @staticmethod
    def _read_stdout(stream: BinaryIO, items: queue.Queue[_ReaderItem]) -> None:
        try:
            while True:
                line = stream.readline(MAX_PROTOCOL_LINE_BYTES + 1)
                if not line:
                    items.put(_STREAM_CLOSED)
                    return
                if len(line) > MAX_PROTOCOL_LINE_BYTES:
                    items.put(_LINE_TOO_LARGE)
                    return
                items.put(line)
        except (OSError, ValueError):
            items.put(_STREAM_FAILED)

    @staticmethod
    def _stop_process(
        process: subprocess.Popen[bytes],
        process_group_id: int | None = None,
        windows_process_tree: WindowsProcessTree | None = None,
    ) -> None:
        if windows_process_tree is not None:
            try:
                stopped = windows_process_tree.stop(process)
            except WindowsProcessTreeError:
                stopped = False
                windows_process_tree.close()
            if not stopped:
                raise AppServerError(
                    "Unable to verify Codex App Server process tree cleanup."
                )
            return
        if process_group_id is not None and AppServerClient._stop_posix_process_group(
            process,
            process_group_id,
        ):
            return
        if process.poll() is not None:
            return
        try:
            process.wait(timeout=_PROCESS_STOP_GRACE_SECONDS)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            process.terminate()
            process.wait(timeout=_PROCESS_STOP_GRACE_SECONDS)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                return
        try:
            process.wait(timeout=_PROCESS_STOP_GRACE_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass

    @staticmethod
    def _dedicated_posix_process_group_id(process: subprocess.Popen[bytes]) -> int | None:
        get_process_group = getattr(os, "getpgid", None)
        get_session = getattr(os, "getsid", None)
        if not callable(get_process_group) or not callable(get_session):
            return None
        try:
            process_group_id = get_process_group(process.pid)
            session_id = get_session(process.pid)
        except OSError:
            return None
        if not isinstance(process_group_id, int) or not isinstance(session_id, int):
            return None
        if process_group_id != process.pid or session_id != process.pid:
            return None
        return process_group_id

    @staticmethod
    def _stop_posix_process_group(
        process: subprocess.Popen[bytes],
        process_group_id: int,
    ) -> bool:
        signal_group = getattr(os, "killpg", None)
        if not callable(signal_group):
            return False
        try:
            process.wait(timeout=_PROCESS_STOP_GRACE_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            signal_group(process_group_id, _POSIX_TERMINATE_SIGNAL)
        except ProcessLookupError:
            AppServerClient._reap_process(process)
            return True
        except OSError:
            return False
        if not AppServerClient._wait_for_posix_process_group(process_group_id):
            try:
                signal_group(process_group_id, _POSIX_KILL_SIGNAL)
            except ProcessLookupError:
                pass
            except OSError:
                return False
            AppServerClient._wait_for_posix_process_group(process_group_id)
        AppServerClient._reap_process(process)
        return True

    @staticmethod
    def _wait_for_posix_process_group(process_group_id: int) -> bool:
        signal_group = getattr(os, "killpg", None)
        if not callable(signal_group):
            return False
        deadline = time.monotonic() + _PROCESS_STOP_GRACE_SECONDS
        while True:
            try:
                signal_group(process_group_id, 0)
            except ProcessLookupError:
                return True
            except OSError:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))

    @staticmethod
    def _reap_process(process: subprocess.Popen[bytes]) -> None:
        try:
            process.wait(timeout=_PROCESS_STOP_GRACE_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass

def _result_object(result: object | None, operation: str) -> Mapping[str, object]:
    if not isinstance(result, dict) or not all(isinstance(key, str) for key in result):
        raise AppServerError(f"Codex App Server returned an invalid {operation} result.")
    return cast(dict[str, object], result)


def _thread_id(result: Mapping[str, object], operation: str) -> str:
    thread = result.get("thread")
    if not isinstance(thread, dict):
        raise AppServerError(f"Codex App Server {operation} result is missing the thread.")
    thread_data = cast(dict[str, object], thread)
    thread_id = thread_data.get("id")
    if not isinstance(thread_id, str) or not thread_id:
        raise AppServerError(f"Codex App Server {operation} result is missing the thread ID.")
    return thread_id


def _approval_request(message: RpcServerRequest) -> AppServerApprovalRequest:
    kinds = {
        AppServerRequestMethod.COMMAND_APPROVAL.value: AppServerApprovalKind.COMMAND,
        AppServerRequestMethod.FILE_CHANGE_APPROVAL.value: AppServerApprovalKind.FILE_CHANGE,
        AppServerRequestMethod.PERMISSIONS_APPROVAL.value: AppServerApprovalKind.PERMISSIONS,
        AppServerRequestMethod.TOOL_INPUT.value: AppServerApprovalKind.TOOL_INPUT,
    }
    kind = kinds.get(message.method, AppServerApprovalKind.UNKNOWN)
    action = "Review a Codex request"
    target: str | None = None
    decisions: tuple[str, ...] = ()
    if kind is AppServerApprovalKind.COMMAND:
        action = _optional_request_string(message.params, "command") or "Execute a command"
        target = _optional_request_string(message.params, "cwd")
        decisions = ("accept", "acceptForSession", "decline", "cancel")
    elif kind is AppServerApprovalKind.FILE_CHANGE:
        action = "Apply file changes"
        target = _optional_request_string(message.params, "grantRoot")
        decisions = ("accept", "acceptForSession", "decline", "cancel")
    elif kind is AppServerApprovalKind.PERMISSIONS:
        action = "Grant additional permissions"
        target = _optional_request_string(message.params, "cwd")
    elif kind is AppServerApprovalKind.TOOL_INPUT:
        action = "Provide requested tool input"
    return AppServerApprovalRequest(
        message.request_id,
        kind,
        message.method,
        action,
        target,
        _optional_request_string(message.params, "reason"),
        decisions,
        _optional_request_string(message.params, "threadId"),
        _optional_request_string(message.params, "turnId"),
        _optional_request_string(message.params, "itemId"),
    )


def _optional_request_string(params: Mapping[str, object], name: str) -> str | None:
    value = params.get(name)
    return value if isinstance(value, str) and value else None


def _consume_turn_notification(
    notification: RpcNotification,
    *,
    thread_id: str,
    turn_id: str,
    deltas: list[str],
    on_agent_delta: Callable[[str], None] | None,
    on_item_started: Callable[[str], None] | None,
    on_item_completed: Callable[[str], None] | None,
) -> AppServerTurnResult | None:
    params = notification.params
    notification_thread = params.get("threadId")
    notification_turn = params.get("turnId")
    if notification.method == _AGENT_MESSAGE_DELTA_METHOD:
        if notification_thread == thread_id and notification_turn == turn_id:
            delta = params.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
                if on_agent_delta is not None:
                    on_agent_delta(delta)
        return None
    if notification.method in {_ITEM_STARTED_METHOD, _ITEM_COMPLETED_METHOD}:
        if notification_thread != thread_id or notification_turn != turn_id:
            return None
        item = params.get("item")
        if not isinstance(item, dict):
            return None
        item_id = cast(dict[str, object], item).get("id")
        if not isinstance(item_id, str) or not item_id:
            return None
        callback = (
            on_item_started
            if notification.method == _ITEM_STARTED_METHOD
            else on_item_completed
        )
        if callback is not None:
            callback(item_id)
        return None
    if notification.method != _TURN_COMPLETED_METHOD:
        return None
    if notification_thread != thread_id:
        return None
    turn = params.get("turn")
    if not isinstance(turn, dict):
        raise AppServerError("Codex App Server turn/completed is missing the turn.")
    turn_data = cast(dict[str, object], turn)
    completed_turn_id = turn_data.get("id")
    if completed_turn_id != turn_id:
        return None
    status_value = turn_data.get("status")
    try:
        status = AppServerTurnStatus(status_value)
    except (ValueError, TypeError):
        raise AppServerError("Codex App Server returned an unknown turn status.") from None
    items = turn_data.get("items")
    if not isinstance(items, list):
        raise AppServerError("Codex App Server completed turn is missing items.")
    messages: list[str] = []
    completed_item_ids: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_data = cast(dict[str, object], item)
        item_id = item_data.get("id")
        if isinstance(item_id, str):
            completed_item_ids.append(item_id)
        if item_data.get("type") == "agentMessage":
            text = item_data.get("text")
            if isinstance(text, str):
                messages.append(text)
    message = messages[-1].strip() if messages else "".join(deltas).strip()
    failure_code: str | None = None
    failure_message: str | None = None
    error = turn_data.get("error")
    if isinstance(error, dict):
        error_data = cast(dict[str, object], error)
        codex_error = error_data.get("codexErrorInfo")
        if isinstance(codex_error, str) and _SAFE_ERROR_CODE.fullmatch(codex_error):
            failure_code = codex_error
        elif isinstance(codex_error, dict) and codex_error:
            candidate = next(iter(codex_error))
            if isinstance(candidate, str) and _SAFE_ERROR_CODE.fullmatch(candidate):
                failure_code = candidate
        message_value = error_data.get("message")
        if isinstance(message_value, str) and message_value.strip():
            failure_message = message_value.strip()[:8000]
    return AppServerTurnResult(
        thread_id=thread_id,
        turn_id=turn_id,
        status=status,
        message=message,
        completed_item_ids=tuple(completed_item_ids),
        failure_code=failure_code,
        failure_message=failure_message,
    )


def _executable_command(executable: str) -> list[str]:
    command = [executable, "app-server", "--listen", "stdio://"]
    if not sys.platform.startswith("win") or Path(executable).suffix.casefold() not in {
        ".bat",
        ".cmd",
    }:
        return command
    command_line = subprocess.list2cmdline(command)
    return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command_line]


def _create_windows_process_tree() -> WindowsProcessTree:
    try:
        return create_windows_process_tree()
    except WindowsProcessTreeError:
        raise AppServerTransientError(
            "Unable to establish App Server process-tree ownership."
        ) from None
