"""Server A side of DG-AGENT-RUNTIME-V3: the runner-agent gateway.

Runners dial **in** over WebSocket with their `dar_` credential (INV-AGENT-1);
this module owns the live connection registry, relays A2A-shaped frames
(`dispatch_center.agent_protocol`) between Studio sessions and the runner
hosting them, and persists every session event, task state and permission
prompt in SQLite so the browser and the socket are never the truth
(INV-STATE-1, INV-AGENT-2).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from app.assistant_tokens import issue_assistant_turn_token, revoke_assistant_turn_token
from app.audit import AuditActor, append_audit
from app.identity import hash_secret, parse_agent_runner_token
from dispatch_center import agent_protocol as protocol

log = logging.getLogger(__name__)

SendFrame = Callable[[str], Awaitable[None]]
DEFAULT_REQUEST_TIMEOUT_SEC = 30.0
SESSION_EVENT_KINDS = frozenset(set(protocol.EVENT_KINDS) | {"user_text", "status", "permission", "permission_decision", "runner"})


class AgentRunnerAuthError(Exception):
    pass


def authenticate_agent_runner(db: Any, raw_token: Optional[str]) -> Any:
    """Resolve a `dar_` credential to an active runner or raise (fail closed)."""

    if not raw_token:
        raise AgentRunnerAuthError("missing credential")
    try:
        parse_agent_runner_token(raw_token)
    except ValueError as exc:
        raise AgentRunnerAuthError("malformed credential") from exc
    runner = db.get_agent_runner_by_secret_hash(hash_secret(raw_token))
    if runner is None or not runner.is_active:
        raise AgentRunnerAuthError("unknown or revoked credential")
    return runner


@dataclass
class RunnerConnection:
    runner: Any
    send: SendFrame
    connected_at: str
    sessions: set[str] = field(default_factory=set)


@dataclass
class _DiffWaiter:
    future: "asyncio.Future[dict[str, Any]]"


class AgentGateway:
    """In-memory connection registry (disposable cache) over durable SQLite rows."""

    def __init__(
        self,
        db: Any,
        *,
        audit_path: str,
        permission_timeout_sec: float = 300.0,
        request_timeout_sec: float = DEFAULT_REQUEST_TIMEOUT_SEC,
        dispatch_base_url: str = "",
        mcp_max_calls: int = 8,
        session_token_ttl_sec: int = 8 * 3600,
    ) -> None:
        self.db = db
        self.audit_path = audit_path
        self.permission_timeout_sec = permission_timeout_sec
        self.request_timeout_sec = request_timeout_sec
        self.dispatch_base_url = dispatch_base_url
        self.mcp_max_calls = mcp_max_calls
        self.session_token_ttl_sec = session_token_ttl_sec
        self._session_tokens: dict[str, str] = {}
        self.connections: dict[str, RunnerConnection] = {}
        self._subscribers: dict[str, set["asyncio.Queue[dict[str, Any]]"]] = {}
        self._diff_waiters: dict[str, _DiffWaiter] = {}

    # ---------------------------------------------------------------- runners
    def connected_runner_ids(self) -> set[str]:
        return set(self.connections)

    def connection_for_server(self, server_name: str) -> Optional[RunnerConnection]:
        for conn in self.connections.values():
            if conn.runner.server_name == server_name:
                return conn
        return None

    async def attach(self, runner: Any, send: SendFrame) -> RunnerConnection:
        previous = self.connections.get(runner.id)
        conn = RunnerConnection(runner=runner, send=send, connected_at=_now_iso())
        if previous is not None:
            conn.sessions = set(previous.sessions)
        self.connections[runner.id] = conn
        self.db.touch_agent_runner(runner.id)
        self._audit("agent_runner_connected", {"runner_id": runner.id, "server": runner.server_name})
        return conn

    async def detach(self, runner_id: str) -> None:
        conn = self.connections.pop(runner_id, None)
        if conn is None:
            return
        for session_id in list(conn.sessions):
            # Unreachable runner: the session is unknown, never failed (INV-AGENT-1).
            await self._set_state(session_id, "unknown", detail="runner disconnected")
        self._audit("agent_runner_disconnected", {"runner_id": runner_id, "server": conn.runner.server_name})

    # ---------------------------------------------------------------- inbound
    async def handle_runner_frame(self, runner_id: str, raw: str) -> None:
        conn = self.connections.get(runner_id)
        if conn is None:
            return
        try:
            frame = protocol.parse(raw, expected=protocol.RUNNER_METHODS)
        except protocol.ProtocolError as exc:
            log.warning("runner %s sent an invalid frame: %s", runner_id, exc)
            return
        params = frame.params
        if frame.method == protocol.M_HELLO:
            card = params.get("agent_card") if isinstance(params.get("agent_card"), dict) else {}
            self.db.touch_agent_runner(
                runner_id,
                agent_version=str(params.get("version") or "")[:32] or None,
                protocol_version=str(params.get("protocol") or "")[:32] or None,
                agent_card=card,
            )
            await conn.send(protocol.notification(protocol.M_HELLO_ACK, {"runner_id": runner_id}))
            return
        if frame.method == protocol.M_HEARTBEAT:
            self.db.touch_agent_runner(runner_id)
            return
        try:
            session_id = protocol.require_id(params, "session_id")
        except protocol.ProtocolError:
            return
        if not self._session_belongs_to(session_id, runner_id):
            log.warning("runner %s reported on session %s it does not host", runner_id, session_id)
            return
        if frame.method == protocol.M_SESSION_STATUS:
            state = str(params.get("state") or "")
            if state in protocol.TASK_STATES:
                await self._set_state(session_id, state, detail=params.get("detail"))
            return
        if frame.method == protocol.M_SESSION_EVENT:
            event = params.get("event")
            if not isinstance(event, dict) or event.get("kind") not in protocol.EVENT_KINDS:
                return
            await self._record(session_id, str(event["kind"]), event)
            if event.get("kind") == "result":
                self.db.upsert_agent_session_runtime(
                    session_id,
                    sdk_session_id=event.get("sdk_session_id") if isinstance(event.get("sdk_session_id"), str) else None,
                    cost_usd=float(event["total_cost_usd"]) if isinstance(event.get("total_cost_usd"), (int, float)) else None,
                )
            return
        if frame.method == protocol.M_PERMISSION_REQUEST:
            try:
                request_id = protocol.require_id(params, "request_id")
            except protocol.ProtocolError:
                return
            tool_input = params.get("tool_input") if isinstance(params.get("tool_input"), dict) else {}
            inserted = self.db.insert_agent_permission_request(
                request_id=request_id,
                session_id=session_id,
                tool_name=str(params.get("tool_name") or "?"),
                tool_input=tool_input,
                summary=str(params.get("summary") or ""),
                reason=str(params.get("reason") or ""),
                allow_pattern=params.get("allow_pattern") if isinstance(params.get("allow_pattern"), str) else None,
            )
            if inserted:
                self._audit("agent_permission_requested", {"session_id": session_id, "request_id": request_id, "tool": str(params.get("tool_name") or "?"), "summary": str(params.get("summary") or "")[:200]})
                await self._record(
                    session_id,
                    "permission",
                    {
                        "kind": "permission",
                        "request_id": request_id,
                        "tool_name": params.get("tool_name"),
                        "summary": params.get("summary"),
                        "reason": params.get("reason"),
                        "allow_pattern": params.get("allow_pattern"),
                        "tool_input": tool_input,
                        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=self.permission_timeout_sec)).isoformat(),
                    },
                )
            return
        if frame.method == protocol.M_SESSION_DIFF_RESULT:
            waiter = self._diff_waiters.pop(session_id, None)
            if waiter is not None and not waiter.future.done():
                waiter.future.set_result({k: v for k, v in params.items() if k != "session_id"})
            return

    # --------------------------------------------------------------- outbound
    async def open_session(self, session_id: str, *, actor_id: Optional[str] = None, audit_actor: Optional[AuditActor] = None) -> dict[str, Any]:
        session = self.db.get_agent_session(session_id)
        if session is None:
            raise LookupError("session not found")
        runtime = self.db.get_agent_session_runtime(session_id) or {}
        runner_id = runtime.get("runner_id")
        conn = self.connections.get(runner_id) if runner_id else None
        if conn is None:
            await self._set_state(session_id, "unknown", detail="runner not connected")
            raise ConnectionError("runner not connected")
        project = self._project(session.project_id)
        version = self.db.get_project_version(session.base_version_id) if session.base_version_id else None
        if project is None or version is None:
            raise LookupError("project or base version missing")
        source = None
        for instance in self.db.list_project_instances(project["name"]):
            if instance.server == conn.runner.server_name and instance.path:
                source = instance.path
                break
        params: dict[str, Any] = {
            "session_id": session_id,
            "project": project["name"],
            "base_commit": version.git_commit,
            "source": source,
        }
        if runtime.get("sdk_session_id"):
            params["resume"] = runtime["sdk_session_id"]
        if self.dispatch_base_url and actor_id:
            # DG-ASSISTANT-TOOLS v1 T-2/T-3 reused for Studio sessions: one `dat_`
            # token bound to the person who started the session; the runner keeps
            # it in a 0600 file next to the workspace and the MCP bridge sends it
            # as X-Auth-Token. Revoked when the session closes; TTL-bounded anyway.
            previous = self._session_tokens.pop(session_id, None)
            if previous:
                revoke_assistant_turn_token(self.db, token_id=previous, audit_path=self.audit_path, audit_actor=audit_actor)
            issued = issue_assistant_turn_token(
                self.db,
                actor_id=actor_id,
                turn_ref=f"session:{session_id}",
                ttl_sec=self.session_token_ttl_sec,
                audit_path=self.audit_path,
                audit_actor=audit_actor,
            )
            self._session_tokens[session_id] = issued.token_id
            params["mcp"] = {
                "dispatch_base_url": self.dispatch_base_url,
                "token": issued.raw_token,
                "max_calls": int(self.mcp_max_calls),
                "source": "assistant",
            }
        conn.sessions.add(session_id)
        await self._set_state(session_id, "submitted", detail="open requested")
        await conn.send(protocol.notification(protocol.M_SESSION_OPEN, params))
        return params

    async def send_message(self, session_id: str, text: str, *, actor_id: Optional[str]) -> None:
        conn = self._require_connection(session_id)
        await self._record(session_id, "user_text", {"kind": "user_text", "text": text, "actor_id": actor_id})
        await conn.send(protocol.notification(protocol.M_SESSION_MESSAGE, {"session_id": session_id, "text": text}))

    async def interrupt(self, session_id: str) -> None:
        conn = self._require_connection(session_id)
        await conn.send(protocol.notification(protocol.M_SESSION_INTERRUPT, {"session_id": session_id}))

    async def close_session(self, session_id: str, *, reason: Optional[str] = None) -> None:
        for conn in self.connections.values():
            if session_id in conn.sessions:
                conn.sessions.discard(session_id)
                try:
                    await conn.send(protocol.notification(protocol.M_SESSION_CLOSE, {"session_id": session_id}))
                except Exception:  # noqa: BLE001 - closing a session on a dead socket is fine
                    pass
        token_id = self._session_tokens.pop(session_id, None)
        if token_id:
            revoke_assistant_turn_token(self.db, token_id=token_id, audit_path=self.audit_path)
        self.db.close_agent_session(session_id, reason=reason)
        await self._set_state(session_id, "completed", detail=reason or "closed")

    async def request_diff(self, session_id: str) -> dict[str, Any]:
        conn = self._require_connection(session_id)
        waiter = _DiffWaiter(future=asyncio.get_running_loop().create_future())
        self._diff_waiters[session_id] = waiter
        await conn.send(protocol.notification(protocol.M_SESSION_DIFF, {"session_id": session_id}))
        try:
            return await asyncio.wait_for(waiter.future, timeout=self.request_timeout_sec)
        except asyncio.TimeoutError:
            self._diff_waiters.pop(session_id, None)
            return {"ok": False, "unreachable": True, "patch": "", "status": [], "truncated": False}

    async def decide_permission(self, session_id: str, request_id: str, *, allow: bool, allow_pattern: Optional[str], actor: Optional[AuditActor], actor_id: Optional[str]) -> bool:
        request = self.db.get_agent_permission_request(request_id)
        if request is None or request["session_id"] != session_id:
            raise LookupError("permission request not found")
        pattern = allow_pattern if allow and allow_pattern and allow_pattern == request.get("allow_pattern") else None
        if not self.db.decide_agent_permission_request(request_id, status="allowed" if allow else "denied", decided_by_actor_id=actor_id, decision_allow_pattern=pattern):
            return False
        self._audit("agent_permission_decided", {"session_id": session_id, "request_id": request_id, "decision": "allow" if allow else "deny", "allow_pattern": pattern}, actor=actor)
        await self._record(session_id, "permission_decision", {"kind": "permission_decision", "request_id": request_id, "decision": "allow" if allow else "deny", "allow_pattern": pattern, "actor_id": actor_id})
        conn = self._require_connection(session_id)
        await conn.send(
            protocol.notification(
                protocol.M_PERMISSION_DECISION,
                {"session_id": session_id, "request_id": request_id, "decision": "allow" if allow else "deny", "allow_pattern": pattern},
            )
        )
        return True

    # ------------------------------------------------------------ subscribers
    def subscribe(self, session_id: str) -> "asyncio.Queue[dict[str, Any]]":
        queue: "asyncio.Queue[dict[str, Any]]" = asyncio.Queue(maxsize=1000)
        self._subscribers.setdefault(session_id, set()).add(queue)
        return queue

    def unsubscribe(self, session_id: str, queue: "asyncio.Queue[dict[str, Any]]") -> None:
        subscribers = self._subscribers.get(session_id)
        if subscribers is not None:
            subscribers.discard(queue)
            if not subscribers:
                self._subscribers.pop(session_id, None)

    # ---------------------------------------------------------------- helpers
    def _project(self, project_id: str) -> Optional[dict[str, Any]]:
        with self.db.cursor() as cur:
            row = cur.execute("SELECT id, name FROM projects WHERE id = ?", (project_id,)).fetchone()
        return dict(row) if row else None

    def _session_belongs_to(self, session_id: str, runner_id: str) -> bool:
        runtime = self.db.get_agent_session_runtime(session_id)
        return bool(runtime and runtime.get("runner_id") == runner_id)

    def _require_connection(self, session_id: str) -> RunnerConnection:
        runtime = self.db.get_agent_session_runtime(session_id) or {}
        conn = self.connections.get(runtime.get("runner_id") or "")
        if conn is None or session_id not in conn.sessions:
            raise ConnectionError("session is not open on a connected runner")
        return conn

    async def _set_state(self, session_id: str, state: str, *, detail: Optional[str] = None) -> None:
        self.db.upsert_agent_session_runtime(session_id, task_state=state)
        await self._record(session_id, "status", {"kind": "status", "state": state, "detail": (detail or "")[:500]})

    async def _record(self, session_id: str, kind: str, payload: dict[str, Any]) -> None:
        seq = self.db.next_agent_session_seq(session_id)
        self.db.append_agent_session_event(session_id, seq, kind, payload)
        event = {"seq": seq, "kind": kind, "payload": payload, "created_at": _now_iso()}
        for queue in list(self._subscribers.get(session_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def _audit(self, action: str, params: dict[str, Any], *, actor: Optional[AuditActor] = None) -> None:
        try:
            append_audit(action, params, path=self.audit_path, actor=actor)
        except Exception:  # noqa: BLE001 - audit is best-effort (INV-AUDIT-2)
            pass


def ensure_agent_gateway(app_state: Any) -> AgentGateway:
    """The one gateway per running AppState (disposable cache over SQLite rows)."""

    gateway = getattr(app_state, "agent_gateway", None)
    if gateway is None:
        config = app_state.config
        gateway = AgentGateway(
            app_state.db,
            audit_path=config.audit_path,
            permission_timeout_sec=float(getattr(config, "assistant_turn_token_ttl_sec", 150) or 150) * 2,
            dispatch_base_url=str(getattr(config, "assistant_tools_dispatch_base_url", "") or ""),
            mcp_max_calls=int(getattr(config, "assistant_tools_max_calls", 8) or 8),
        )
        app_state.agent_gateway = gateway
    return gateway


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_request_id() -> str:
    return uuid.uuid4().hex


__all__ = ["AgentGateway", "AgentRunnerAuthError", "ensure_agent_gateway", "RunnerConnection", "SESSION_EVENT_KINDS", "authenticate_agent_runner"]
