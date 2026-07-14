from __future__ import annotations

import json

from devloop.execution.protocol import (
    JsonLineCodec,
    RpcNotification,
    RpcRequest,
    RpcRequestTracker,
    RpcResponse,
    RpcServerRequest,
    RpcServerResponse,
)


def test_request_framing_is_utf8_jsonl_without_jsonrpc_header() -> None:
    message = RpcRequest(
        request_id=7,
        method="initialize",
        params={"clientInfo": {"title": "Δοκιμή"}},
    )

    encoded = JsonLineCodec.encode(message)

    assert encoded.endswith("\n")
    assert "Δοκιμή" in encoded
    assert json.loads(encoded) == {
        "id": 7,
        "method": "initialize",
        "params": {"clientInfo": {"title": "Δοκιμή"}},
    }


def test_server_request_response_uses_the_original_request_id() -> None:
    encoded = JsonLineCodec.encode(RpcServerResponse(19, {"decision": "accept"}))

    assert json.loads(encoded) == {"id": 19, "result": {"decision": "accept"}}


def test_server_request_string_id_is_preserved_in_its_response() -> None:
    request = JsonLineCodec.decode(
        '{"id":"approval-7","method":"item/commandExecution/requestApproval","params":{}}'
    )

    assert request == RpcServerRequest(
        request_id="approval-7",
        method="item/commandExecution/requestApproval",
        params={},
    )
    assert isinstance(request, RpcServerRequest)
    encoded = JsonLineCodec.encode(RpcServerResponse(request.request_id, {"decision": "decline"}))
    assert json.loads(encoded) == {"id": "approval-7", "result": {"decision": "decline"}}


def test_request_tracker_correlates_responses_around_notifications() -> None:
    tracker = RpcRequestTracker()
    initialize = tracker.create_request("initialize", {})
    account = tracker.create_request("account/read", {"refreshToken": False})

    tracker.route(
        JsonLineCodec.decode(
            '{"method":"remoteControl/status/changed","params":{"installationId":"secret"}}'
        )
    )
    tracker.route(JsonLineCodec.decode('{"id":1,"result":{"account":null}}'))
    tracker.route(JsonLineCodec.decode('{"id":0,"result":{"platformFamily":"windows"}}'))

    assert (initialize.request_id, account.request_id) == (0, 1)
    assert tracker.notification_methods == ("remoteControl/status/changed",)
    assert tracker.take_response(initialize.request_id) == RpcResponse(
        request_id=0,
        result={"platformFamily": "windows"},
    )
    assert tracker.take_response(account.request_id) == RpcResponse(
        request_id=1,
        result={"account": None},
    )
    assert all(not isinstance(message, RpcNotification) for message in tracker.buffered_responses)
