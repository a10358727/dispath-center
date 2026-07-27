"""Bounded, transport-agnostic JSON-RPC session layer for a future Codex
app-server adapter (D1 first slice; see docs/DECISIONS.md and
docs/AI_ENGINEERING_DECISION_GATE.md §D1).

This module intentionally does not launch, connect to, or know how to reach
any real process.  ``AppServerTransport`` is a narrow protocol a caller must
inject; the only implementations in this repository are fake in-process
peers used by tests (see ``tests/test_codex_app_server.py``).  Nothing here
is wired into the existing turn lifecycle, the outer Coding Runner Job
executor, or any approval flow — that bridging is explicitly a later,
separately reviewed slice.

The message layer models a plausible JSON-RPC 2.0 session shape (typed
request/response/notification, id correlation, an ``initialize`` handshake
that pins an exact protocol version and capability set).  It is not a claim
of wire compatibility with any specific real Codex app-server release: no
such protocol has been reviewed or pinned yet.  Version/capability drift at
handshake time fails closed rather than negotiating or degrading.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Mapping, Protocol


#: Placeholder pin for this bounded slice's own internal protocol shape.
#: A real adapter activation requires an explicitly reviewed and pinned
#: Codex CLI/app-server version plus its exact capability set (D1); this is
#: not that value and must not be presented as one.
REVIEWED_PROTOCOL_VERSION = "dispatch-codex-app-server-session-v1"
REVIEWED_CAPABILITIES = frozenset(
    {"turn/start", "turn/resume", "turn/cancel", "turn/event", "command/approve"}
)

_JSONRPC_VERSION = "2.0"


class AppServerProtocolError(RuntimeError):
    """The peer's message shape, version, or capability set is not trusted.

    Deliberately carries no message-content detail in its public string:
    callers must not surface raw peer bytes into logs or API responses.
    """


class AppServerCallError(RuntimeError):
    """The peer returned a well-formed JSON-RPC error response."""

    def __init__(self, code: int, message: str):
        super().__init__(f"app-server call failed: {code}")
        self.code = code
        self.message = message


def _require(condition: bool) -> None:
    if not condition:
        raise AppServerProtocolError("malformed or untrusted app-server message")


@dataclass(frozen=True)
class JsonRpcRequest:
    id: str | int
    method: str
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.id, bool) or not isinstance(self.id, (str, int)):
            raise ValueError("request id must be a string or integer")
        if isinstance(self.id, str) and not self.id:
            raise ValueError("request id must not be empty")
        if not isinstance(self.method, str) or not self.method:
            raise ValueError("method must be a non-empty string")
        if not isinstance(self.params, Mapping):
            raise ValueError("params must be a mapping")

    def to_wire(self) -> dict[str, Any]:
        return {
            "jsonrpc": _JSONRPC_VERSION,
            "id": self.id,
            "method": self.method,
            "params": dict(self.params),
        }


@dataclass(frozen=True)
class JsonRpcNotification:
    method: str
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.method, str) or not self.method:
            raise ValueError("method must be a non-empty string")
        if not isinstance(self.params, Mapping):
            raise ValueError("params must be a mapping")

    def to_wire(self) -> dict[str, Any]:
        return {
            "jsonrpc": _JSONRPC_VERSION,
            "method": self.method,
            "params": dict(self.params),
        }


@dataclass(frozen=True)
class JsonRpcResponse:
    id: str | int
    result: Mapping[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return {"jsonrpc": _JSONRPC_VERSION, "id": self.id, "result": dict(self.result)}


@dataclass(frozen=True)
class JsonRpcErrorResponse:
    id: str | int
    code: int
    message: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "jsonrpc": _JSONRPC_VERSION,
            "id": self.id,
            "error": {"code": self.code, "message": self.message},
        }


def _parse_incoming(raw: Mapping[str, Any]) -> Any:
    """Return a typed request/response/error/notification, or fail closed.

    Only the fixed shapes this session speaks are accepted.  An unknown
    combination of fields (e.g. both ``result`` and ``error``, or neither
    ``id`` nor ``method``) is untrusted and rejected rather than guessed at.
    """

    _require(isinstance(raw, Mapping))
    _require(raw.get("jsonrpc") == _JSONRPC_VERSION)
    has_id = "id" in raw
    has_method = "method" in raw
    has_result = "result" in raw
    has_error = "error" in raw
    _require(sum((has_result, has_error)) <= 1)
    if has_method and not has_result and not has_error:
        method = raw.get("method")
        params = raw.get("params", {})
        _require(isinstance(params, Mapping))
        if has_id:
            request_id = raw.get("id")
            _require(
                isinstance(request_id, (str, int)) and not isinstance(request_id, bool)
            )
            _require(isinstance(method, str) and bool(method))
            return JsonRpcRequest(id=request_id, method=method, params=params)
        _require(isinstance(method, str) and bool(method))
        return JsonRpcNotification(method=method, params=params)
    if has_id and has_result and not has_method:
        request_id = raw.get("id")
        result = raw.get("result", {})
        _require(
            isinstance(request_id, (str, int)) and not isinstance(request_id, bool)
        )
        _require(isinstance(result, Mapping))
        return JsonRpcResponse(id=request_id, result=result)
    if has_id and has_error and not has_method:
        request_id = raw.get("id")
        error = raw.get("error")
        _require(
            isinstance(request_id, (str, int)) and not isinstance(request_id, bool)
        )
        _require(isinstance(error, Mapping))
        code = error.get("code")
        message = error.get("message")
        _require(isinstance(code, int) and not isinstance(code, bool))
        _require(isinstance(message, str))
        return JsonRpcErrorResponse(id=request_id, code=code, message=message)
    _require(False)


class AppServerTransport(Protocol):
    """Narrow async duplex-message protocol a caller must inject.

    No implementation in this repository talks to a real process; the only
    implementations are fake in-process peers constructed by tests.
    """

    async def send(self, message: Mapping[str, Any]) -> None: ...

    async def receive(self) -> Mapping[str, Any]: ...


@dataclass
class CodexAppServerSession:
    """One handshake-scoped session over an injected transport.

    Every public method fails closed on a malformed peer message rather than
    guessing.  This class never retries, falls back, or widens what it will
    accept after a protocol error — a caller must open a new session.
    """

    transport: AppServerTransport
    _handshaked: bool = field(default=False, init=False, repr=False)
    _next_request_id: int = field(default=1, init=False, repr=False)
    _pending_command_approvals: dict[str | int, JsonRpcRequest] = field(
        default_factory=dict, init=False, repr=False
    )

    def _allocate_request_id(self) -> int:
        request_id = self._next_request_id
        self._next_request_id += 1
        return request_id

    async def _call(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        if not self._handshaked:
            raise AppServerProtocolError("session used before a successful handshake")
        request_id = self._allocate_request_id()
        await self.transport.send(
            JsonRpcRequest(id=request_id, method=method, params=params).to_wire()
        )
        raw = await self.transport.receive()
        parsed = _parse_incoming(raw)
        if isinstance(parsed, JsonRpcErrorResponse):
            _require(parsed.id == request_id)
            raise AppServerCallError(parsed.code, parsed.message)
        _require(isinstance(parsed, JsonRpcResponse))
        _require(parsed.id == request_id)
        return parsed.result

    async def handshake(self) -> None:
        """Send ``initialize`` and fail closed on any version/capability drift."""

        if self._handshaked:
            raise AppServerProtocolError("session already handshaked")
        request_id = self._allocate_request_id()
        await self.transport.send(
            JsonRpcRequest(
                id=request_id,
                method="initialize",
                params={
                    "protocol_version": REVIEWED_PROTOCOL_VERSION,
                    "capabilities": sorted(REVIEWED_CAPABILITIES),
                },
            ).to_wire()
        )
        raw = await self.transport.receive()
        parsed = _parse_incoming(raw)
        if isinstance(parsed, JsonRpcErrorResponse):
            _require(parsed.id == request_id)
            raise AppServerCallError(parsed.code, parsed.message)
        _require(isinstance(parsed, JsonRpcResponse))
        _require(parsed.id == request_id)
        peer_version = parsed.result.get("protocol_version")
        peer_capabilities = parsed.result.get("capabilities")
        if (
            peer_version != REVIEWED_PROTOCOL_VERSION
            or not isinstance(peer_capabilities, list)
            or set(peer_capabilities) != REVIEWED_CAPABILITIES
        ):
            raise AppServerProtocolError(
                "app-server protocol version or capability set does not match "
                "the reviewed pin"
            )
        self._handshaked = True

    async def start_turn(
        self, *, thread_id: str, instruction: str
    ) -> Mapping[str, Any]:
        return await self._call(
            "turn/start", {"thread_id": thread_id, "instruction": instruction}
        )

    async def resume_turn(
        self, *, thread_id: str, instruction: str
    ) -> Mapping[str, Any]:
        return await self._call(
            "turn/resume", {"thread_id": thread_id, "instruction": instruction}
        )

    async def cancel_turn(self, *, thread_id: str, turn_id: str) -> None:
        await self._call("turn/cancel", {"thread_id": thread_id, "turn_id": turn_id})

    async def stream_events(
        self, *, thread_id: str, turn_id: str
    ) -> AsyncIterator[Mapping[str, Any]]:
        """Yield peer notifications for this turn until a terminal event.

        A peer *request* named ``command/approve`` arriving mid-stream is
        queued (by its wire id) rather than yielded as an ordinary event —
        the caller must answer it through
        ``build_command_approval_handle``/``respond_to_command_approval``
        before the peer will continue.  This session never answers such a
        request on its own.
        """

        if not self._handshaked:
            raise AppServerProtocolError("session used before a successful handshake")
        while True:
            raw = await self.transport.receive()
            parsed = _parse_incoming(raw)
            if isinstance(parsed, JsonRpcRequest):
                _require(parsed.method == "command/approve")
                _require(parsed.params.get("thread_id") == thread_id)
                _require(parsed.params.get("turn_id") == turn_id)
                self._pending_command_approvals[parsed.id] = parsed
                continue
            _require(isinstance(parsed, JsonRpcNotification))
            _require(parsed.method == "turn/event")
            _require(parsed.params.get("thread_id") == thread_id)
            _require(parsed.params.get("turn_id") == turn_id)
            yield parsed.params
            if parsed.params.get("terminal") is True:
                return

    def build_command_approval_handle(
        self,
        request_id: str | int,
        *,
        engineering_task_id: str,
        attempt_number: int,
        parent_approval_id: int,
    ):
        """Correlate a queued peer request to an opaque, immutable handle.

        Import is local to avoid a module-level dependency from this
        transport-agnostic session layer onto the coding-agent registry
        module (which imports back into the approval/executor stack).
        """

        from app.coding_agents import CodingAgentCommandApprovalHandle

        pending = self._pending_command_approvals.get(request_id)
        if pending is None:
            raise AppServerProtocolError(
                "no queued command/approve request for this id"
            )
        params = pending.params
        thread_id = params.get("thread_id")
        turn_id = params.get("turn_id")
        item_id = params.get("item_id")
        command_digest = params.get("command_digest")
        working_directory = params.get("working_directory")
        provider_approval_id = params.get("provider_approval_id")
        if (
            not isinstance(thread_id, str)
            or not isinstance(turn_id, str)
            or not isinstance(item_id, str)
            or not isinstance(command_digest, str)
            or not isinstance(working_directory, str)
            or not (provider_approval_id is None or isinstance(provider_approval_id, str))
        ):
            raise AppServerProtocolError("command/approve params are not well-formed")
        return CodingAgentCommandApprovalHandle(
            request_id=pending.id,
            engineering_task_id=engineering_task_id,
            attempt_number=attempt_number,
            parent_approval_id=parent_approval_id,
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            provider_approval_id=provider_approval_id,
            command_digest=command_digest,
            working_directory=working_directory,
        )

    async def respond_to_command_approval(self, *, handle, decision) -> None:
        """Send the human decision back as the queued request's JSON-RPC response.

        ``handle``/``decision`` are typed as
        ``app.coding_agents.CodingAgentCommandApprovalHandle`` /
        ``CodingAgentCommandApprovalDecision`` (imported lazily; see
        ``build_command_approval_handle``).
        """

        from app.coding_agents import CodingAgentCommandApprovalDecision

        if not isinstance(decision, CodingAgentCommandApprovalDecision):
            raise ValueError("decision must be a CodingAgentCommandApprovalDecision")
        pending = self._pending_command_approvals.get(handle.request_id)
        if pending is None or pending.id != handle.request_id:
            raise AppServerProtocolError(
                "handle does not correlate to a queued command/approve request"
            )
        if (
            pending.params.get("thread_id") != handle.thread_id
            or pending.params.get("turn_id") != handle.turn_id
            or pending.params.get("item_id") != handle.item_id
            or pending.params.get("command_digest") != handle.command_digest
            or pending.params.get("working_directory") != handle.working_directory
        ):
            raise AppServerProtocolError(
                "handle no longer matches the queued command/approve request"
            )
        del self._pending_command_approvals[handle.request_id]
        await self.transport.send(
            JsonRpcResponse(
                id=handle.request_id, result={"decision": decision.value}
            ).to_wire()
        )
