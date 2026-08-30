"""Claude Agent SDK adapter: one ``SessionHost`` per AgentSession.

The SDK is imported lazily (``client_factory``/``types`` are injectable) so the
package, its tests and ``--check`` never require the 200 MB engine to be
present.  SDK messages are serialised into the protocol's event vocabulary by
duck typing on class names and attributes.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from dispatch_agent.permissions import PermissionDecision, allow_pattern_for, decide_tool_use

EventSink = Callable[[dict[str, Any]], Awaitable[None]]
PermissionSink = Callable[["PermissionRequest"], Awaitable[None]]
MAX_EVENT_TEXT = 8000


@dataclass
class PermissionRequest:
    request_id: str
    session_id: str
    tool_name: str
    tool_input: dict[str, Any]
    summary: str
    reason: str
    allow_pattern: Optional[str]
    future: "asyncio.Future[tuple[bool, Optional[str]]]" = field(repr=False)


@dataclass(frozen=True)
class SdkTypes:
    """The two result classes ``can_use_tool`` must return (injected)."""

    allow: Callable[..., Any]
    deny: Callable[..., Any]


def _clip(text: Any, limit: int = MAX_EVENT_TEXT) -> str:
    value = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    return value if len(value) <= limit else value[:limit] + "…"


def serialize_sdk_message(message: Any) -> list[dict[str, Any]]:
    """Map one SDK message object to zero or more protocol events."""

    name = type(message).__name__
    events: list[dict[str, Any]] = []
    if name == "StreamEvent":
        event = getattr(message, "event", None) or {}
        delta = event.get("delta") if isinstance(event, dict) else None
        if isinstance(delta, dict) and delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
            events.append({"kind": "text_delta", "text": delta["text"], "parent_tool_use_id": getattr(message, "parent_tool_use_id", None)})
        return events
    if name == "AssistantMessage":
        for block in getattr(message, "content", None) or []:
            block_name = type(block).__name__
            if block_name == "TextBlock":
                events.append({"kind": "assistant_text", "text": _clip(getattr(block, "text", ""))})
            elif block_name == "ThinkingBlock":
                events.append({"kind": "thinking", "chars": len(getattr(block, "thinking", "") or "")})
            elif block_name == "ToolUseBlock":
                events.append({"kind": "tool_use", "tool_use_id": getattr(block, "id", None), "name": getattr(block, "name", ""), "input": _bounded_input(getattr(block, "input", {}))})
        return events
    if name == "UserMessage":
        content = getattr(message, "content", None)
        if isinstance(content, list):
            for block in content:
                if type(block).__name__ == "ToolResultBlock":
                    events.append({"kind": "tool_result", "tool_use_id": getattr(block, "tool_use_id", None), "is_error": bool(getattr(block, "is_error", False)), "content": _clip(getattr(block, "content", "") or "")})
        return events
    if name == "ResultMessage":
        events.append(
            {
                "kind": "result",
                "is_error": bool(getattr(message, "is_error", False)),
                "num_turns": getattr(message, "num_turns", None),
                "duration_ms": getattr(message, "duration_ms", None),
                "total_cost_usd": getattr(message, "total_cost_usd", None),
                "usage": getattr(message, "usage", None),
                "sdk_session_id": getattr(message, "session_id", None),
                "text": _clip(getattr(message, "result", "") or ""),
            }
        )
        return events
    if name.endswith("SystemMessage") or name == "SystemMessage":
        events.append({"kind": "system", "subtype": getattr(message, "subtype", ""), "data": _bounded_input(getattr(message, "data", {}) or {})})
    return events


def _bounded_input(value: Any, limit: int = MAX_EVENT_TEXT) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return value if len(text) <= limit else {"_truncated": True, "preview": text[:limit]}


class SessionHost:
    """Owns one SDK client, its receive loop and its pending permission prompts."""

    def __init__(
        self,
        *,
        session_id: str,
        workspace: Path,
        validation_allowlist: tuple[str, ...],
        client_factory: Callable[[Any], Any],
        options_factory: Callable[..., Any],
        types: SdkTypes,
        on_event: EventSink,
        on_permission_request: PermissionSink,
        permission_timeout_sec: float,
    ) -> None:
        self.session_id = session_id
        self.workspace = workspace
        self.validation_allowlist = validation_allowlist
        self._client_factory = client_factory
        self._options_factory = options_factory
        self._types = types
        self._on_event = on_event
        self._on_permission_request = on_permission_request
        self._permission_timeout = permission_timeout_sec
        self._client: Any = None
        self._receive_task: Optional[asyncio.Task[None]] = None
        self._pending: dict[str, PermissionRequest] = {}
        self.session_allow_patterns: set[str] = set()
        self.sdk_session_id: Optional[str] = None
        self.seq = 0
        self.turn_active = False

    async def start(self, *, resume: Optional[str] = None) -> None:
        options = self._options_factory(cwd=str(self.workspace), can_use_tool=self.can_use_tool, resume=resume)
        self._client = self._client_factory(options)
        await self._client.connect()
        self._receive_task = asyncio.create_task(self._receive_loop())

    async def send(self, text: str) -> None:
        if self._client is None:
            raise RuntimeError("session not started")
        self.turn_active = True
        await self._client.query(text)

    async def interrupt(self) -> None:
        if self._client is not None:
            await self._client.interrupt()

    async def close(self) -> None:
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_result((False, None))
        self._pending.clear()
        if self._receive_task is not None:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._receive_task = None
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    async def _receive_loop(self) -> None:
        async for message in self._client.receive_messages():
            for event in serialize_sdk_message(message):
                if event["kind"] == "result":
                    self.turn_active = False
                    if event.get("sdk_session_id"):
                        self.sdk_session_id = event["sdk_session_id"]
                self.seq += 1
                event["seq"] = self.seq
                await self._on_event(event)

    async def can_use_tool(self, tool_name: str, tool_input: dict[str, Any], context: Any = None) -> Any:
        decision: PermissionDecision = decide_tool_use(
            tool_name,
            tool_input,
            workspace=self.workspace,
            validation_allowlist=self.validation_allowlist,
            session_allow_patterns=self.session_allow_patterns,
        )
        if decision.action == "allow":
            return self._types.allow()
        if decision.action == "deny":
            return self._types.deny(message=decision.reason)
        request = PermissionRequest(
            request_id=uuid.uuid4().hex,
            session_id=self.session_id,
            tool_name=tool_name,
            tool_input=tool_input,
            summary=decision.summary,
            reason=decision.reason,
            allow_pattern=allow_pattern_for(tool_input.get("command", "")) if tool_name == "Bash" else None,
            future=asyncio.get_running_loop().create_future(),
        )
        self._pending[request.request_id] = request
        await self._on_permission_request(request)
        try:
            allowed, pattern = await asyncio.wait_for(request.future, timeout=self._permission_timeout)
        except asyncio.TimeoutError:
            allowed, pattern = False, None
            decision_reason = "逾時未決定，已拒絕"
        else:
            decision_reason = "session 擁有者拒絕" if not allowed else ""
        finally:
            self._pending.pop(request.request_id, None)
        if allowed:
            if pattern:
                self.session_allow_patterns.add(pattern)
            return self._types.allow()
        return self._types.deny(message=decision_reason)

    def resolve_permission(self, request_id: str, *, allow: bool, allow_pattern: Optional[str] = None) -> bool:
        pending = self._pending.get(request_id)
        if pending is None or pending.future.done():
            return False
        pattern = allow_pattern if allow and allow_pattern and allow_pattern == pending.allow_pattern else None
        pending.future.set_result((allow, pattern))
        return True


__all__ = ["PermissionRequest", "SdkTypes", "SessionHost", "serialize_sdk_message"]
