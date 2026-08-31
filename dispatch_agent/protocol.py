"""Wire protocol between a runner and Server A (JSON-RPC 2.0 over WebSocket).

The vocabulary follows A2A: a session turn is a *task* whose state moves
``submitted → working → (input-required ↔ working) → completed | failed |
canceled``; ``input-required`` is a workspace permission prompt.  The runner
only ever **dials out**; Server A never connects in (INV-AGENT-1).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

PROTOCOL_VERSION = "dispatch-agent/1"

TASK_STATES = ("submitted", "working", "input-required", "completed", "failed", "canceled")
EVENT_KINDS = ("text_delta", "assistant_text", "thinking", "tool_use", "tool_result", "result", "system", "error", "context")

# runner -> server
M_HELLO = "runner/hello"
M_HEARTBEAT = "runner/heartbeat"
M_SESSION_EVENT = "session/event"
M_SESSION_STATUS = "session/status"
M_PERMISSION_REQUEST = "permission/request"
M_SESSION_DIFF_RESULT = "session/diff/result"
# server -> runner
M_HELLO_ACK = "runner/hello/ack"
M_SESSION_OPEN = "session/open"
M_SESSION_MESSAGE = "session/message"
M_SESSION_INTERRUPT = "session/interrupt"
M_SESSION_CLOSE = "session/close"
M_SESSION_DIFF = "session/diff"
M_SESSION_CONFIGURE = "session/configure"
M_SESSION_FILES = "session/files"
M_SESSION_FILES_RESULT = "session/files/result"
M_PERMISSION_DECISION = "permission/decision"

RUNNER_METHODS = frozenset({M_HELLO, M_HEARTBEAT, M_SESSION_EVENT, M_SESSION_STATUS, M_PERMISSION_REQUEST, M_SESSION_DIFF_RESULT, M_SESSION_FILES_RESULT})
SERVER_METHODS = frozenset({M_HELLO_ACK, M_SESSION_OPEN, M_SESSION_MESSAGE, M_SESSION_INTERRUPT, M_SESSION_CLOSE, M_SESSION_DIFF, M_SESSION_FILES, M_SESSION_CONFIGURE, M_PERMISSION_DECISION})

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}\Z")
MAX_FRAME_BYTES = 256 * 1024
MAX_TEXT_CHARS = 64 * 1024


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class Parsed:
    method: str
    params: dict[str, Any]
    id: Optional[str] = None


def notification(method: str, params: dict[str, Any]) -> str:
    return json.dumps({"jsonrpc": "2.0", "method": method, "params": params}, ensure_ascii=False, separators=(",", ":"))


def request(method: str, params: dict[str, Any], request_id: str) -> str:
    if not _ID_RE.match(request_id):
        raise ProtocolError("invalid request id")
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}, ensure_ascii=False, separators=(",", ":"))


def parse(text: str, *, expected: frozenset[str]) -> Parsed:
    """Parse one frame; anything unexpected is a ``ProtocolError`` (fail closed)."""

    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_FRAME_BYTES:
        raise ProtocolError("frame too large or not text")
    try:
        data = json.loads(text)
    except ValueError:
        raise ProtocolError("frame is not JSON") from None
    if not isinstance(data, dict) or data.get("jsonrpc") != "2.0":
        raise ProtocolError("frame is not JSON-RPC 2.0")
    method = data.get("method")
    if not isinstance(method, str) or method not in expected:
        raise ProtocolError(f"unexpected method {method!r}")
    params = data.get("params", {})
    if not isinstance(params, dict):
        raise ProtocolError("params must be an object")
    frame_id = data.get("id")
    if frame_id is not None and (not isinstance(frame_id, str) or not _ID_RE.match(frame_id)):
        raise ProtocolError("invalid id")
    return Parsed(method=method, params=params, id=frame_id)


def require_id(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ProtocolError(f"{key} missing or invalid")
    return value


def require_text(params: dict[str, Any], key: str, *, max_chars: int = MAX_TEXT_CHARS) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"{key} missing")
    if len(value) > max_chars:
        raise ProtocolError(f"{key} too long")
    return value


def hello(runner_name: str, version: str, agent_card: dict[str, Any]) -> str:
    return notification(M_HELLO, {"protocol": PROTOCOL_VERSION, "runner_name": runner_name, "version": version, "agent_card": agent_card})


def heartbeat(runner_name: str, active_sessions: int) -> str:
    return notification(M_HEARTBEAT, {"runner_name": runner_name, "active_sessions": active_sessions})


def session_event(session_id: str, seq: int, event: dict[str, Any]) -> str:
    kind = event.get("kind")
    if kind not in EVENT_KINDS:
        raise ProtocolError(f"unknown event kind {kind!r}")
    return notification(M_SESSION_EVENT, {"session_id": session_id, "seq": seq, "event": event})


def session_status(session_id: str, state: str, *, detail: Optional[str] = None, turn_no: Optional[int] = None, workspace: Optional[str] = None) -> str:
    if state not in TASK_STATES:
        raise ProtocolError(f"unknown task state {state!r}")
    params: dict[str, Any] = {"session_id": session_id, "state": state}
    if detail:
        params["detail"] = detail[:500]
    if turn_no is not None:
        params["turn_no"] = int(turn_no)
    if workspace:
        # the runner-side worktree path, reported once at open so Server A can
        # run the checkpoint/bundle pipeline against it (Phase 1b)
        params["workspace"] = workspace[:300]
    return notification(M_SESSION_STATUS, params)


def permission_request(session_id: str, request_id: str, tool_name: str, tool_input: dict[str, Any], summary: str, reason: str, allow_pattern: Optional[str]) -> str:
    return notification(
        M_PERMISSION_REQUEST,
        {
            "session_id": session_id,
            "request_id": request_id,
            "tool_name": tool_name,
            "tool_input": _bounded(tool_input),
            "summary": summary[:500],
            "reason": reason[:200],
            "allow_pattern": allow_pattern,
        },
    )


def _bounded(value: Any, limit: int = 8000) -> Any:
    text = json.dumps(value, ensure_ascii=False)
    if len(text) <= limit:
        return value
    return {"_truncated": True, "preview": text[:limit]}


__all__ = [name for name in dir() if name.startswith(("M_", "TASK_", "EVENT_", "RUNNER_", "SERVER_", "PROTOCOL", "MAX_"))] + [
    "Parsed",
    "ProtocolError",
    "heartbeat",
    "hello",
    "notification",
    "parse",
    "permission_request",
    "request",
    "require_id",
    "require_text",
    "session_event",
    "session_status",
]
