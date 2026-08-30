"""JSON-RPC frames between runner and Server A."""

from __future__ import annotations

import json

import pytest

from dispatch_agent import protocol


def test_hello_heartbeat_event_status_frames_round_trip():
    frame = protocol.hello("worker_5090_106", "0.1.0", {"capabilities": {"gpus": []}})
    parsed = protocol.parse(frame, expected=protocol.RUNNER_METHODS)
    assert parsed.method == protocol.M_HELLO and parsed.params["runner_name"] == "worker_5090_106"
    assert parsed.params["protocol"] == protocol.PROTOCOL_VERSION
    parsed = protocol.parse(protocol.heartbeat("r", 2), expected=protocol.RUNNER_METHODS)
    assert parsed.params == {"runner_name": "r", "active_sessions": 2}
    parsed = protocol.parse(protocol.session_event("s1", 3, {"kind": "assistant_text", "text": "hi"}), expected=protocol.RUNNER_METHODS)
    assert parsed.params["seq"] == 3 and parsed.params["event"]["kind"] == "assistant_text"
    parsed = protocol.parse(protocol.session_status("s1", "input-required", detail="pip install rich", turn_no=2), expected=protocol.RUNNER_METHODS)
    assert parsed.params["state"] == "input-required" and parsed.params["turn_no"] == 2
    with pytest.raises(protocol.ProtocolError):
        protocol.session_status("s1", "exploded")
    with pytest.raises(protocol.ProtocolError):
        protocol.session_event("s1", 1, {"kind": "nope"})


def test_parse_fails_closed_on_foreign_methods_and_bad_frames():
    server_frame = protocol.notification(protocol.M_SESSION_MESSAGE, {"session_id": "s1", "text": "hi"})
    with pytest.raises(protocol.ProtocolError, match="unexpected method"):
        protocol.parse(server_frame, expected=protocol.RUNNER_METHODS)
    assert protocol.parse(server_frame, expected=protocol.SERVER_METHODS).params["text"] == "hi"
    for bad in ("not json", "[]", json.dumps({"jsonrpc": "1.0", "method": protocol.M_HELLO}), json.dumps({"jsonrpc": "2.0", "method": protocol.M_HELLO, "params": []}), json.dumps({"jsonrpc": "2.0", "method": protocol.M_HELLO, "id": "bad id!"})):
        with pytest.raises(protocol.ProtocolError):
            protocol.parse(bad, expected=protocol.RUNNER_METHODS)
    with pytest.raises(protocol.ProtocolError, match="too large"):
        protocol.parse("x" * (protocol.MAX_FRAME_BYTES + 1), expected=protocol.RUNNER_METHODS)


def test_permission_request_bounds_tool_input_and_require_helpers():
    frame = protocol.permission_request("s1", "r1", "Bash", {"command": "x" * 20000}, "summary", "reason", "x:*")
    params = protocol.parse(frame, expected=protocol.RUNNER_METHODS).params
    assert params["tool_input"]["_truncated"] is True and len(json.dumps(params)) < 12000
    assert protocol.require_id(params, "request_id") == "r1"
    with pytest.raises(protocol.ProtocolError):
        protocol.require_id({"session_id": "has space"}, "session_id")
    with pytest.raises(protocol.ProtocolError):
        protocol.require_text({"text": " "}, "text")
    with pytest.raises(protocol.ProtocolError):
        protocol.require_text({"text": "y" * 10}, "text", max_chars=5)
