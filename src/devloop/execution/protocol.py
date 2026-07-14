from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

MAX_PROTOCOL_LINE_BYTES = 4 * 1024 * 1024
MIN_REQUEST_ID = -(2**63)
MAX_REQUEST_ID = 2**63 - 1
RequestId = int | str


class ProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class RpcRequest:
    request_id: int
    method: str
    params: Mapping[str, object]


@dataclass(frozen=True)
class RpcNotification:
    method: str
    params: Mapping[str, object]


@dataclass(frozen=True)
class RpcServerRequest:
    request_id: RequestId
    method: str
    params: Mapping[str, object]


@dataclass(frozen=True)
class RpcError:
    code: int
    message: str


@dataclass(frozen=True)
class RpcResponse:
    request_id: int
    result: object | None = None
    error: RpcError | None = None


@dataclass(frozen=True)
class RpcServerResponse:
    request_id: RequestId
    result: Mapping[str, object]


InboundMessage = RpcNotification | RpcServerRequest | RpcResponse
OutboundMessage = RpcNotification | RpcRequest | RpcServerResponse


class JsonLineCodec:
    """Codex App Server's headerless JSON-RPC framing over stdio."""

    @staticmethod
    def encode(message: OutboundMessage) -> str:
        if isinstance(message, RpcServerResponse):
            return json.dumps(
                {"id": message.request_id, "result": dict(message.result)},
                ensure_ascii=False,
                separators=(",", ":"),
            ) + "\n"
        payload: dict[str, object] = {
            "method": message.method,
            "params": dict(message.params),
        }
        if isinstance(message, RpcRequest):
            payload["id"] = message.request_id
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"

    @staticmethod
    def decode(line: str) -> InboundMessage:
        if len(line.encode("utf-8")) > MAX_PROTOCOL_LINE_BYTES:
            raise ProtocolError("App Server message exceeds the supported size limit.")
        try:
            decoded: object = json.loads(line)
        except json.JSONDecodeError as error:
            raise ProtocolError("App Server sent malformed JSON.") from error
        if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
            raise ProtocolError("App Server message must be a JSON object.")
        payload = cast(dict[str, object], decoded)

        method = payload.get("method")
        request_id = payload.get("id")
        params = _object_params(payload.get("params", {}))
        if isinstance(method, str):
            if request_id is None:
                return RpcNotification(method=method, params=params)
            return RpcServerRequest(
                request_id=_server_request_id(request_id),
                method=method,
                params=params,
            )

        response_id = _response_request_id(request_id)
        error_value = payload.get("error")
        if error_value is not None:
            if not isinstance(error_value, dict):
                raise ProtocolError("App Server error response has an invalid error object.")
            error_payload = cast(dict[str, object], error_value)
            code = error_payload.get("code")
            message = error_payload.get("message")
            if isinstance(code, bool) or not isinstance(code, int) or not isinstance(message, str):
                raise ProtocolError("App Server error response is missing code or message.")
            return RpcResponse(
                request_id=response_id,
                error=RpcError(code=code, message=message),
            )
        if "result" not in payload:
            raise ProtocolError("App Server response is missing result or error.")
        return RpcResponse(request_id=response_id, result=payload["result"])


class RpcRequestTracker:
    """Allocates request IDs and routes interleaved inbound messages safely."""

    def __init__(self) -> None:
        self._next_request_id = 0
        self._pending: set[int] = set()
        self._responses: dict[int, RpcResponse] = {}
        self._notification_methods: list[str] = []
        self._server_requests: list[RpcServerRequest] = []

    def create_request(self, method: str, params: Mapping[str, object]) -> RpcRequest:
        request = RpcRequest(
            request_id=self._next_request_id,
            method=method,
            params=params,
        )
        self._next_request_id += 1
        self._pending.add(request.request_id)
        return request

    def route(self, message: InboundMessage) -> None:
        if isinstance(message, RpcNotification):
            self._notification_methods.append(message.method)
            return
        if isinstance(message, RpcServerRequest):
            self._server_requests.append(message)
            return
        if message.request_id not in self._pending:
            raise ProtocolError(f"Unexpected App Server response ID: {message.request_id}")
        if message.request_id in self._responses:
            raise ProtocolError(f"Duplicate App Server response ID: {message.request_id}")
        self._responses[message.request_id] = message

    def take_response(self, request_id: int) -> RpcResponse | None:
        response = self._responses.pop(request_id, None)
        if response is not None:
            self._pending.discard(request_id)
        return response

    @property
    def notification_methods(self) -> tuple[str, ...]:
        return tuple(self._notification_methods)

    @property
    def server_requests(self) -> tuple[RpcServerRequest, ...]:
        return tuple(self._server_requests)

    @property
    def buffered_responses(self) -> tuple[RpcResponse, ...]:
        return tuple(self._responses[key] for key in sorted(self._responses))


def _server_request_id(value: object) -> RequestId:
    if isinstance(value, str):
        return value
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not MIN_REQUEST_ID <= value <= MAX_REQUEST_ID
    ):
        raise ProtocolError("App Server message has an invalid request ID.")
    return value


def _response_request_id(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_REQUEST_ID
    ):
        raise ProtocolError("App Server message has an invalid request ID.")
    return value


def _object_params(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ProtocolError("App Server message params must be a JSON object.")
    return cast(dict[str, object], value)
