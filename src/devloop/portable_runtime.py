from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from itertools import count
from queue import Empty
from queue import Queue
from threading import RLock, get_ident
from typing import Any, Iterator, TextIO


class PortableRuntimeEventKind(str, Enum):
    CHOICE_REQUESTED = "CHOICE_REQUESTED"
    INPUT_REQUESTED = "INPUT_REQUESTED"
    INTERACTION_COMPLETED = "INTERACTION_COMPLETED"
    SCREEN_UPDATED = "SCREEN_UPDATED"
    OUTPUT_WRITTEN = "OUTPUT_WRITTEN"


class _PortableInteractionKind(str, Enum):
    PREVIEW = "PREVIEW"
    RESPOND = "RESPOND"


@dataclass(frozen=True)
class PortableRuntimeEvent:
    kind: PortableRuntimeEventKind
    request_id: int
    options: tuple[tuple[str, str], ...] = ()
    default_key: str = ""
    cancel_key: str | None = None
    prompt: str = ""
    input_history: tuple[str, ...] = ()
    shortcuts: tuple[tuple[str, str], ...] = ()
    content: str = ""
    is_error: bool = False


class PortableRuntimeBridge:
    def __init__(self) -> None:
        self._event_queue: Queue[PortableRuntimeEvent] = Queue()
        self._responses: dict[int, Queue[tuple[_PortableInteractionKind, str]]] = {}
        self._request_ids = count(1)
        self._response_lock = RLock()

    def choose(
        self,
        options: Sequence[tuple[str, str]],
        *,
        default_key: str,
        cancel_key: str | None,
        render: Callable[[str], None],
        shortcuts: Mapping[str, str] | None = None,
    ) -> str:
        request_id = next(self._request_ids)
        response: Queue[tuple[_PortableInteractionKind, str]] = Queue()
        with self._response_lock:
            self._responses[request_id] = response
        self._event_queue.put(
            PortableRuntimeEvent(
                kind=PortableRuntimeEventKind.CHOICE_REQUESTED,
                request_id=request_id,
                options=tuple(options),
                default_key=default_key,
                cancel_key=cancel_key,
                shortcuts=tuple((shortcuts or {}).items()),
            )
        )
        render(default_key)
        while True:
            interaction, value = response.get()
            if interaction is _PortableInteractionKind.PREVIEW:
                render(value)
                continue
            with self._response_lock:
                self._responses.pop(request_id, None)
            self._publish_interaction_completed(request_id)
            return value

    def next_event(self, *, timeout: float | None = None) -> PortableRuntimeEvent:
        return self._event_queue.get(timeout=timeout)

    def try_next_event(self) -> PortableRuntimeEvent | None:
        try:
            return self._event_queue.get_nowait()
        except Empty:
            return None

    def read_line(self, prompt: str, *, history: Sequence[str] = ()) -> str:
        request_id = next(self._request_ids)
        response: Queue[tuple[_PortableInteractionKind, str]] = Queue()
        with self._response_lock:
            self._responses[request_id] = response
        self._event_queue.put(
            PortableRuntimeEvent(
                kind=PortableRuntimeEventKind.INPUT_REQUESTED,
                request_id=request_id,
                prompt=prompt,
                input_history=tuple(history),
            )
        )
        _interaction, value = response.get()
        with self._response_lock:
            self._responses.pop(request_id, None)
        self._publish_interaction_completed(request_id)
        return value

    def show_screen(self, content: str) -> None:
        self._event_queue.put(
            PortableRuntimeEvent(
                kind=PortableRuntimeEventKind.SCREEN_UPDATED,
                request_id=0,
                content=content,
            )
        )

    def write_output(self, content: str, *, is_error: bool) -> None:
        if not content:
            return
        self._event_queue.put(
            PortableRuntimeEvent(
                kind=PortableRuntimeEventKind.OUTPUT_WRITTEN,
                request_id=0,
                content=content,
                is_error=is_error,
            )
        )

    def respond(self, request_id: int, value: str) -> None:
        self._send_interaction(request_id, _PortableInteractionKind.RESPOND, value)

    def preview(self, request_id: int, value: str) -> None:
        self._send_interaction(request_id, _PortableInteractionKind.PREVIEW, value)

    def _send_interaction(
        self,
        request_id: int,
        interaction: _PortableInteractionKind,
        value: str,
    ) -> None:
        with self._response_lock:
            try:
                response = self._responses[request_id]
            except KeyError as error:
                raise ValueError(
                    f"Unknown portable interaction request: {request_id}"
                ) from error
        response.put((interaction, value))

    def _publish_interaction_completed(self, request_id: int) -> None:
        self._event_queue.put(
            PortableRuntimeEvent(
                kind=PortableRuntimeEventKind.INTERACTION_COMPLETED,
                request_id=request_id,
            )
        )


class PortableRoutedStream:
    def __init__(
        self,
        bridge: PortableRuntimeBridge,
        terminal_stream: TextIO,
        *,
        application_thread_id: int,
        is_error: bool,
    ) -> None:
        self._bridge = bridge
        self._terminal_stream = terminal_stream
        self._application_thread_id = application_thread_id
        self._is_error = is_error

    def write(self, content: str) -> int:
        if get_ident() == self._application_thread_id:
            return self._terminal_stream.write(content)
        self._bridge.write_output(content, is_error=self._is_error)
        return len(content)

    def flush(self) -> None:
        if get_ident() == self._application_thread_id:
            self._terminal_stream.flush()

    def isatty(self) -> bool:
        if get_ident() != self._application_thread_id:
            return False
        return self._terminal_stream.isatty()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._terminal_stream, name)


@contextmanager
def route_worker_output(bridge: PortableRuntimeBridge) -> Iterator[None]:
    """Keep Textual on the terminal while routing worker output into the app."""
    application_thread_id = get_ident()
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    sys.stdout = PortableRoutedStream(
        bridge,
        original_stdout,
        application_thread_id=application_thread_id,
        is_error=False,
    )
    sys.stderr = PortableRoutedStream(
        bridge,
        original_stderr,
        application_thread_id=application_thread_id,
        is_error=True,
    )
    try:
        yield
    finally:
        sys.stdout = original_stdout
        sys.stderr = original_stderr


_active_bridge: PortableRuntimeBridge | None = None
_plain_mode_active: ContextVar[bool] = ContextVar(
    "devloop_portable_plain_mode_active",
    default=False,
)


def active_portable_runtime() -> PortableRuntimeBridge | None:
    return _active_bridge


def portable_plain_mode_active() -> bool:
    return _plain_mode_active.get()


@contextmanager
def portable_plain_mode_session() -> Iterator[None]:
    token = _plain_mode_active.set(True)
    try:
        yield
    finally:
        _plain_mode_active.reset(token)


@contextmanager
def portable_runtime_session(bridge: PortableRuntimeBridge) -> Iterator[None]:
    global _active_bridge
    if _active_bridge is not None:
        raise RuntimeError("A Portable Application Shell session is already active")
    _active_bridge = bridge
    try:
        yield
    finally:
        _active_bridge = None
