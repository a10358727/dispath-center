"""Outbound WebSocket client: the runner dials Server A and stays connected."""

from __future__ import annotations

import asyncio
import logging
import random
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from dispatch_agent import __version__
from dispatch_agent import protocol
from dispatch_agent.config import AgentConfig
from dispatch_agent.sdk_adapter import PermissionRequest, SessionHost, SdkTypes
from dispatch_agent.workspace import build_session_context, WorkspaceError, collect_diff, ensure_workspace

log = logging.getLogger("dispatch_agent")

Connector = Callable[[str, dict[str, str]], Awaitable[Any]]


class RunnerClient:
    """One process-wide connection; sessions are multiplexed by ``session_id``."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        agent_card: dict[str, Any],
        connect: Connector,
        session_host_factory: Callable[..., SessionHost],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        bundle_fetcher: Optional[Callable[[str, Path], Awaitable[Path]]] = None,
    ) -> None:
        self.config = config
        self.agent_card = agent_card
        self._connect = connect
        self._session_host_factory = session_host_factory
        self._sleep = sleep
        self._bundle_fetcher = bundle_fetcher
        self.sessions: dict[str, SessionHost] = {}
        self._ws: Any = None
        self._stopping = False
        self._send_lock = asyncio.Lock()

    async def send(self, frame: str) -> None:
        if self._ws is None:
            raise RuntimeError("not connected")
        async with self._send_lock:
            await self._ws.send(frame)

    async def run_forever(self) -> None:
        backoff = 1.0
        while not self._stopping:
            try:
                self._ws = await self._connect(self.config.websocket_url, {"X-Agent-Runner-Token": self.config.credential})
                backoff = 1.0
                await self.send(protocol.hello(self.config.runner_name, __version__, self.agent_card))
                heartbeat = asyncio.create_task(self._heartbeat_loop())
                try:
                    await self._receive_loop()
                finally:
                    heartbeat.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect; unreachable is unknown, not failure
                log.warning("runner connection lost: %s", exc.__class__.__name__)
            self._ws = None
            if self._stopping:
                break
            await self._sleep(backoff + random.uniform(0, 1))
            backoff = min(backoff * 2, 60.0)

    async def stop(self) -> None:
        self._stopping = True
        for host in list(self.sessions.values()):
            await host.close()
        self.sessions.clear()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass

    async def _heartbeat_loop(self) -> None:
        while True:
            await self._sleep(self.config.heartbeat_sec)
            try:
                await self.send(protocol.heartbeat(self.config.runner_name, len(self.sessions)))
            except Exception:  # noqa: BLE001
                return

    async def _receive_loop(self) -> None:
        async for raw in self._ws:
            try:
                frame = protocol.parse(raw, expected=protocol.SERVER_METHODS)
            except protocol.ProtocolError as exc:
                log.warning("dropping frame: %s", exc)
                continue
            await self.handle(frame)

    async def handle(self, frame: protocol.Parsed) -> None:
        params = frame.params
        if frame.method == protocol.M_HELLO_ACK:
            return
        if frame.method == protocol.M_SESSION_OPEN:
            await self._open_session(params)
            return
        session_id = protocol.require_id(params, "session_id")
        host = self.sessions.get(session_id)
        if frame.method == protocol.M_SESSION_MESSAGE:
            if host is None:
                await self.send(protocol.session_status(session_id, "failed", detail="session not open on runner"))
                return
            await self.send(protocol.session_status(session_id, "working"))
            await host.send(protocol.require_text(params, "text"))
        elif frame.method == protocol.M_SESSION_INTERRUPT and host is not None:
            await host.interrupt()
            await self.send(protocol.session_status(session_id, "canceled"))
        elif frame.method == protocol.M_SESSION_CLOSE and host is not None:
            await host.close()
            self.sessions.pop(session_id, None)
            await self.send(protocol.session_status(session_id, "completed", detail="closed"))
        elif frame.method == protocol.M_SESSION_CONFIGURE and host is not None:
            try:
                await host.configure(
                    model=params.get("model") if isinstance(params.get("model"), str) else None,
                    permission_mode=params.get("permission_mode") if isinstance(params.get("permission_mode"), str) else None,
                )
            except (ValueError, RuntimeError) as exc:
                await self.send(protocol.session_status(session_id, "working", detail=f"configure refused: {exc}"[:200]))
        elif frame.method == protocol.M_SESSION_DIFF and host is not None:
            result = await asyncio.to_thread(collect_diff, host.workspace)
            await self.send(protocol.notification(protocol.M_SESSION_DIFF_RESULT, {"session_id": session_id, **result}))
        elif frame.method == protocol.M_PERMISSION_DECISION and host is not None:
            request_id = protocol.require_id(params, "request_id")
            allow = params.get("decision") == "allow"
            pattern = params.get("allow_pattern") if isinstance(params.get("allow_pattern"), str) else None
            host.resolve_permission(request_id, allow=allow, allow_pattern=pattern)

    async def _open_session(self, params: dict[str, Any]) -> None:
        session_id = protocol.require_id(params, "session_id")
        project = protocol.require_text(params, "project", max_chars=64)
        base_commit = protocol.require_text(params, "base_commit", max_chars=40)
        source = params.get("source") if isinstance(params.get("source"), str) else None
        bundle_url = params.get("bundle_url") if isinstance(params.get("bundle_url"), str) else None
        resume = params.get("resume") if isinstance(params.get("resume"), str) else None
        mcp = params.get("mcp") if isinstance(params.get("mcp"), dict) else None
        options = params.get("options") if isinstance(params.get("options"), dict) else None
        if session_id in self.sessions:
            await self.send(protocol.session_status(session_id, "submitted", detail="already open"))
            return
        await self.send(protocol.session_status(session_id, "submitted"))
        try:
            bundle: Optional[Path] = None
            if bundle_url and self._bundle_fetcher is not None:
                bundle = await self._bundle_fetcher(bundle_url, self.config.workspace_root / "bundles" / f"{session_id}.bundle")
            paths = await asyncio.to_thread(
                ensure_workspace,
                root=self.config.workspace_root,
                project=project,
                session_id=session_id,
                base_commit=base_commit,
                source=source,
                bundle=bundle,
            )
        except (WorkspaceError, OSError) as exc:
            await self.send(protocol.session_status(session_id, "failed", detail=f"workspace: {exc}"[:500]))
            return
        try:
            workspace_context = await asyncio.to_thread(build_session_context, paths.repo, paths.session_dir)
        except OSError:
            workspace_context = {}

        async def on_event(event: dict[str, Any]) -> None:
            await self.send(protocol.session_event(session_id, int(event.get("seq", 0)), event))
            if event.get("kind") == "result":
                await self.send(protocol.session_status(session_id, "failed" if event.get("is_error") else "completed", turn_no=None))

        async def on_permission(request: PermissionRequest) -> None:
            await self.send(protocol.session_status(session_id, "input-required", detail=request.summary))
            await self.send(
                protocol.permission_request(
                    session_id, request.request_id, request.tool_name, request.tool_input, request.summary, request.reason, request.allow_pattern
                )
            )

        host = self._session_host_factory(
            session_id=session_id,
            workspace=paths.repo,
            on_event=on_event,
            on_permission_request=on_permission,
        )
        try:
            await host.start(resume=resume, mcp=mcp, options=options, workspace_context=workspace_context)
        except Exception as exc:  # noqa: BLE001
            await self.send(protocol.session_status(session_id, "failed", detail=f"sdk: {exc.__class__.__name__}"))
            return
        self.sessions[session_id] = host
        await self.send(protocol.session_status(session_id, "working", detail="ready"))


def build_session_host_factory(config: AgentConfig, *, options_factory: Callable[..., Any], client_factory: Callable[[Any], Any], types: SdkTypes) -> Callable[..., SessionHost]:
    def factory(*, session_id: str, workspace: Path, on_event: Any, on_permission_request: Any) -> SessionHost:
        return SessionHost(
            session_id=session_id,
            workspace=workspace,
            validation_allowlist=config.validation_allowlist,
            client_factory=client_factory,
            options_factory=options_factory,
            types=types,
            on_event=on_event,
            on_permission_request=on_permission_request,
            permission_timeout_sec=config.permission_timeout_sec,
        )

    return factory


__all__ = ["RunnerClient", "build_session_host_factory"]
