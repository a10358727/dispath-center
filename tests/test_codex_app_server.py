"""Pure tests for the bounded, transport-agnostic JSON-RPC session layer
(D1 first slice; see app/codex_app_server.py and docs/DECISIONS.md D1).

Only an in-process fake transport is used here — this module never launches
or connects to a real process, matching the module's own contract.
"""

import pytest

from app.codex_app_server import (
    AppServerCallError,
    AppServerProtocolError,
    CodexAppServerSession,
    JsonRpcNotification,
    JsonRpcRequest,
    REVIEWED_CAPABILITIES,
    REVIEWED_PROTOCOL_VERSION,
)
from app.coding_agents import (
    CodingAgentCommandApprovalDecision,
    CodingAgentCommandApprovalHandle,
)


class ScriptedTransport:
    """Records every outgoing message and replays scripted incoming ones."""

    def __init__(self, scripted_incoming=()):
        self._incoming = list(scripted_incoming)
        self.sent = []

    def queue(self, message):
        self._incoming.append(message)

    async def send(self, message):
        self.sent.append(message)

    async def receive(self):
        return self._incoming.pop(0)


def _ok_handshake_response(request_id=1):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "protocol_version": REVIEWED_PROTOCOL_VERSION,
            "capabilities": sorted(REVIEWED_CAPABILITIES),
        },
    }


async def _handshaked_session(extra_incoming=()):
    transport = ScriptedTransport([_ok_handshake_response(), *extra_incoming])
    session = CodexAppServerSession(transport=transport)
    await session.handshake()
    return session, transport


@pytest.mark.asyncio
async def test_handshake_succeeds_on_exact_reviewed_pin():
    session, transport = await _handshaked_session()

    assert transport.sent == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocol_version": REVIEWED_PROTOCOL_VERSION,
                "capabilities": sorted(REVIEWED_CAPABILITIES),
            },
        }
    ]


@pytest.mark.asyncio
async def test_handshake_rejects_double_use():
    session, _ = await _handshaked_session()

    with pytest.raises(AppServerProtocolError, match="already handshaked"):
        await session.handshake()


@pytest.mark.asyncio
async def test_handshake_fails_closed_on_protocol_version_drift():
    transport = ScriptedTransport(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "protocol_version": "some-other-version",
                    "capabilities": sorted(REVIEWED_CAPABILITIES),
                },
            }
        ]
    )
    session = CodexAppServerSession(transport=transport)

    with pytest.raises(AppServerProtocolError, match="protocol version"):
        await session.handshake()


@pytest.mark.asyncio
async def test_handshake_fails_closed_on_capability_set_drift():
    transport = ScriptedTransport(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "protocol_version": REVIEWED_PROTOCOL_VERSION,
                    "capabilities": ["turn/start"],
                },
            }
        ]
    )
    session = CodexAppServerSession(transport=transport)

    with pytest.raises(AppServerProtocolError, match="capability"):
        await session.handshake()


@pytest.mark.asyncio
async def test_handshake_surfaces_peer_error_response():
    transport = ScriptedTransport(
        [{"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "nope"}}]
    )
    session = CodexAppServerSession(transport=transport)

    with pytest.raises(AppServerCallError) as excinfo:
        await session.handshake()
    assert excinfo.value.code == -32000
    assert excinfo.value.message == "nope"


@pytest.mark.asyncio
async def test_calls_before_handshake_fail_closed():
    transport = ScriptedTransport()
    session = CodexAppServerSession(transport=transport)

    with pytest.raises(AppServerProtocolError, match="before a successful handshake"):
        await session.start_turn(thread_id="t1", instruction="do work")
    assert transport.sent == []


@pytest.mark.asyncio
async def test_start_turn_round_trips_through_the_peer():
    session, transport = await _handshaked_session(
        [{"jsonrpc": "2.0", "id": 2, "result": {"turn_id": "turn-1"}}]
    )

    result = await session.start_turn(thread_id="thread-1", instruction="do work")

    assert result == {"turn_id": "turn-1"}
    assert transport.sent[-1] == {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "turn/start",
        "params": {"thread_id": "thread-1", "instruction": "do work"},
    }


@pytest.mark.asyncio
async def test_resume_turn_round_trips_through_the_peer():
    session, transport = await _handshaked_session(
        [{"jsonrpc": "2.0", "id": 2, "result": {"turn_id": "turn-1"}}]
    )

    result = await session.resume_turn(thread_id="thread-1", instruction="continue")

    assert result == {"turn_id": "turn-1"}
    assert transport.sent[-1]["method"] == "turn/resume"


@pytest.mark.asyncio
async def test_cancel_turn_round_trips_and_returns_nothing():
    session, transport = await _handshaked_session(
        [{"jsonrpc": "2.0", "id": 2, "result": {}}]
    )

    result = await session.cancel_turn(thread_id="thread-1", turn_id="turn-1")

    assert result is None
    assert transport.sent[-1] == {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "turn/cancel",
        "params": {"thread_id": "thread-1", "turn_id": "turn-1"},
    }


@pytest.mark.asyncio
async def test_call_rejects_mismatched_response_id():
    session, _ = await _handshaked_session(
        [{"jsonrpc": "2.0", "id": 999, "result": {}}]
    )

    with pytest.raises(AppServerProtocolError):
        await session.cancel_turn(thread_id="thread-1", turn_id="turn-1")


@pytest.mark.asyncio
async def test_call_surfaces_peer_error_response():
    session, _ = await _handshaked_session(
        [{"jsonrpc": "2.0", "id": 2, "error": {"code": -32001, "message": "denied"}}]
    )

    with pytest.raises(AppServerCallError) as excinfo:
        await session.cancel_turn(thread_id="thread-1", turn_id="turn-1")
    assert excinfo.value.code == -32001


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"jsonrpc": "1.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0"},
        {"jsonrpc": "2.0", "id": 1, "result": {}, "error": {"code": 1, "message": "x"}},
        {"jsonrpc": "2.0", "id": 1, "result": "not-a-mapping"},
        {"jsonrpc": "2.0", "id": True, "result": {}},
        {"jsonrpc": "2.0", "id": 1, "error": "not-a-mapping"},
        {"jsonrpc": "2.0", "id": 1, "error": {"code": "not-an-int", "message": "x"}},
        {"jsonrpc": "2.0", "method": "turn/event", "params": "not-a-mapping"},
    ],
)
@pytest.mark.asyncio
async def test_malformed_peer_messages_fail_closed(raw):
    session, _ = await _handshaked_session([raw])

    with pytest.raises(AppServerProtocolError):
        await session.cancel_turn(thread_id="thread-1", turn_id="turn-1")


def _turn_event(thread_id, turn_id, *, terminal, payload=None):
    return {
        "jsonrpc": "2.0",
        "method": "turn/event",
        "params": {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "terminal": terminal,
            **(payload or {}),
        },
    }


def _command_approve_request(request_id, thread_id, turn_id, **params):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "command/approve",
        "params": {"thread_id": thread_id, "turn_id": turn_id, **params},
    }


@pytest.mark.asyncio
async def test_stream_events_yields_until_terminal_event():
    session, _ = await _handshaked_session(
        [
            _turn_event("t", "u", terminal=False, payload={"seq": 1}),
            _turn_event("t", "u", terminal=False, payload={"seq": 2}),
            _turn_event("t", "u", terminal=True, payload={"seq": 3}),
        ]
    )

    events = [
        event async for event in session.stream_events(thread_id="t", turn_id="u")
    ]

    assert [event["seq"] for event in events] == [1, 2, 3]


@pytest.mark.asyncio
async def test_stream_events_rejects_wrong_thread_or_turn():
    session, _ = await _handshaked_session(
        [_turn_event("other-thread", "u", terminal=True)]
    )

    with pytest.raises(AppServerProtocolError):
        async for _ in session.stream_events(thread_id="t", turn_id="u"):
            pass


@pytest.mark.asyncio
async def test_stream_events_rejects_unexpected_notification_method():
    session, _ = await _handshaked_session(
        [
            {
                "jsonrpc": "2.0",
                "method": "turn/unexpected",
                "params": {"thread_id": "t", "turn_id": "u"},
            }
        ]
    )

    with pytest.raises(AppServerProtocolError):
        async for _ in session.stream_events(thread_id="t", turn_id="u"):
            pass


@pytest.mark.asyncio
async def test_stream_events_queues_command_approve_instead_of_yielding_it():
    session, _ = await _handshaked_session(
        [
            _command_approve_request(
                7,
                "t",
                "u",
                item_id="item-1",
                command_digest="d" * 64,
                working_directory="/approved/worktree",
                provider_approval_id=None,
            ),
            _turn_event("t", "u", terminal=True, payload={"seq": 1}),
        ]
    )

    events = [
        event async for event in session.stream_events(thread_id="t", turn_id="u")
    ]

    assert [event["seq"] for event in events] == [1]
    assert 7 in session._pending_command_approvals


@pytest.mark.asyncio
async def test_build_and_respond_to_command_approval_round_trip():
    session, transport = await _handshaked_session(
        [
            _command_approve_request(
                7,
                "t",
                "u",
                item_id="item-1",
                command_digest="d" * 64,
                working_directory="/approved/worktree",
                provider_approval_id="prov-1",
            ),
            _turn_event("t", "u", terminal=True),
        ]
    )
    async for _ in session.stream_events(thread_id="t", turn_id="u"):
        pass

    handle = session.build_command_approval_handle(
        7,
        engineering_task_id="task-1",
        attempt_number=1,
        parent_approval_id=42,
    )
    assert isinstance(handle, CodingAgentCommandApprovalHandle)
    assert handle.thread_id == "t"
    assert handle.provider_approval_id == "prov-1"

    await session.respond_to_command_approval(
        handle=handle, decision=CodingAgentCommandApprovalDecision.ACCEPT
    )

    assert transport.sent[-1] == {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {"decision": "accept"},
    }
    # Answered once; a second response for the same id must fail closed.
    with pytest.raises(AppServerProtocolError):
        await session.respond_to_command_approval(
            handle=handle, decision=CodingAgentCommandApprovalDecision.DECLINE
        )


@pytest.mark.asyncio
async def test_build_command_approval_handle_rejects_unknown_request_id():
    session, _ = await _handshaked_session()

    with pytest.raises(AppServerProtocolError, match="no queued"):
        session.build_command_approval_handle(
            999,
            engineering_task_id="task-1",
            attempt_number=1,
            parent_approval_id=42,
        )


@pytest.mark.asyncio
async def test_build_command_approval_handle_rejects_malformed_params():
    session, _ = await _handshaked_session(
        [
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "command/approve",
                "params": {
                    "thread_id": "t",
                    "turn_id": "u",
                    "item_id": "item-1",
                    "command_digest": "d" * 64,
                    "working_directory": 12345,
                    "provider_approval_id": None,
                },
            },
            _turn_event("t", "u", terminal=True),
        ]
    )
    async for _ in session.stream_events(thread_id="t", turn_id="u"):
        pass

    with pytest.raises(AppServerProtocolError, match="not well-formed"):
        session.build_command_approval_handle(
            7,
            engineering_task_id="task-1",
            attempt_number=1,
            parent_approval_id=42,
        )


@pytest.mark.asyncio
async def test_respond_to_command_approval_rejects_decision_type():
    session, transport = await _handshaked_session(
        [
            _command_approve_request(
                7,
                "t",
                "u",
                item_id="item-1",
                command_digest="d" * 64,
                working_directory="/approved/worktree",
                provider_approval_id=None,
            ),
            _turn_event("t", "u", terminal=True),
        ]
    )
    async for _ in session.stream_events(thread_id="t", turn_id="u"):
        pass
    handle = session.build_command_approval_handle(
        7, engineering_task_id="task-1", attempt_number=1, parent_approval_id=42
    )

    sent_before = list(transport.sent)
    with pytest.raises(ValueError, match="CodingAgentCommandApprovalDecision"):
        await session.respond_to_command_approval(handle=handle, decision="accept")
    assert transport.sent == sent_before


def test_jsonrpc_request_rejects_bool_id():
    with pytest.raises(ValueError, match="string or integer"):
        JsonRpcRequest(id=True, method="turn/start")


def test_jsonrpc_request_rejects_empty_string_id():
    with pytest.raises(ValueError, match="must not be empty"):
        JsonRpcRequest(id="", method="turn/start")


def test_jsonrpc_request_rejects_empty_method():
    with pytest.raises(ValueError, match="non-empty string"):
        JsonRpcRequest(id=1, method="")


def test_jsonrpc_request_rejects_non_mapping_params():
    with pytest.raises(ValueError, match="mapping"):
        JsonRpcRequest(id=1, method="turn/start", params="not-a-mapping")


def test_jsonrpc_notification_rejects_empty_method():
    with pytest.raises(ValueError, match="non-empty string"):
        JsonRpcNotification(method="")
