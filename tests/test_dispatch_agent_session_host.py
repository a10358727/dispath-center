"""SessionHost against a duck-typed fake SDK client (no claude-agent-sdk needed)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from dispatch_agent.sdk_adapter import SdkTypes, SessionHost, serialize_sdk_message


class TextBlock:
    def __init__(self, text):
        self.text = text


class ToolUseBlock:
    def __init__(self, id, name, input):
        self.id, self.name, self.input = id, name, input


class ToolResultBlock:
    def __init__(self, tool_use_id, content, is_error=False):
        self.tool_use_id, self.content, self.is_error = tool_use_id, content, is_error


class AssistantMessage:
    def __init__(self, content):
        self.content = content


class UserMessage:
    def __init__(self, content):
        self.content = content


class ResultMessage:
    def __init__(self, **kw):
        self.__dict__.update({"subtype": "success", "duration_ms": 1, "duration_api_ms": 1, "is_error": False, "num_turns": 1, "session_id": "sdk-1", "total_cost_usd": 0.01, "usage": {"input_tokens": 1}, "result": "done"})
        self.__dict__.update(kw)


class StreamEvent:
    def __init__(self, event, parent_tool_use_id=None):
        self.event, self.parent_tool_use_id = event, parent_tool_use_id


class Allow:
    def __init__(self, **kw):
        self.kw = kw


class Deny:
    def __init__(self, message=""):
        self.message = message


def test_serialize_sdk_messages_is_bounded_and_duck_typed():
    events = serialize_sdk_message(AssistantMessage([TextBlock("hi"), ToolUseBlock("t1", "Bash", {"command": "pytest"})]))
    assert [e["kind"] for e in events] == ["assistant_text", "tool_use"]
    assert events[1]["tool_use_id"] == "t1" and events[1]["input"] == {"command": "pytest"}
    big = serialize_sdk_message(UserMessage([ToolResultBlock("t1", "x" * 20000, True)]))[0]
    assert big["kind"] == "tool_result" and big["is_error"] is True and len(big["content"]) < 9000
    assert serialize_sdk_message(StreamEvent({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "h"}})) == [
        {"kind": "text_delta", "text": "h", "parent_tool_use_id": None}
    ]
    assert serialize_sdk_message(StreamEvent({"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{"}})) == []
    result = serialize_sdk_message(ResultMessage())[0]
    assert result["kind"] == "result" and result["sdk_session_id"] == "sdk-1" and result["total_cost_usd"] == 0.01
    assert serialize_sdk_message(SimpleNamespace()) == []


@dataclass
class FakeClient:
    options: Any
    queries: list = field(default_factory=list)
    interrupted: bool = False
    disconnected: bool = False
    script: list = field(default_factory=list)
    permission_calls: list = field(default_factory=list)

    async def connect(self):
        return None

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)

    async def interrupt(self):
        self.interrupted = True

    async def disconnect(self):
        self.disconnected = True

    async def receive_messages(self):
        while True:
            if not self.queries:
                await asyncio.sleep(0.005)
                continue
            self.queries.pop(0)
            for item in self.script:
                if isinstance(item, tuple) and item[0] == "permission":
                    _, tool, tool_input = item
                    result = await self.options["can_use_tool"](tool, tool_input, None)
                    self.permission_calls.append((tool, type(result).__name__, getattr(result, "message", None)))
                else:
                    yield item
            yield ResultMessage()


def _host(tmp_path, script, permission_timeout=0.2):
    events: list = []
    prompts: list = []
    clients: list = []

    async def on_event(event):
        events.append(event)

    async def on_permission(request):
        prompts.append(request)

    def client_factory(options):
        client = FakeClient(options=options, script=script)
        clients.append(client)
        return client

    def options_factory(*, cwd, can_use_tool, resume, mcp_config_path=None):
        return {"cwd": cwd, "can_use_tool": can_use_tool, "resume": resume, "mcp_config_path": mcp_config_path}

    ws = tmp_path / "repo"
    ws.mkdir(exist_ok=True)
    host = SessionHost(
        session_id="s1",
        workspace=ws,
        validation_allowlist=("pytest",),
        client_factory=client_factory,
        options_factory=options_factory,
        types=SdkTypes(allow=Allow, deny=Deny),
        on_event=on_event,
        on_permission_request=on_permission,
        permission_timeout_sec=permission_timeout,
    )
    return host, events, prompts, clients


async def _wait_for(predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_turn_streams_events_and_records_sdk_session_id(tmp_path):
    host, events, prompts, clients = _host(tmp_path, [AssistantMessage([TextBlock("hello")])])
    await host.start(resume="prev")
    assert clients[0].options["resume"] == "prev" and clients[0].options["cwd"] == str(tmp_path / "repo")
    await host.send("say hello")
    await _wait_for(lambda: any(e["kind"] == "result" for e in events))
    assert [e["kind"] for e in events] == ["assistant_text", "result"]
    assert [e["seq"] for e in events] == [1, 2]
    assert host.sdk_session_id == "sdk-1" and host.turn_active is False
    await host.close()
    assert clients[0].disconnected


@pytest.mark.asyncio
async def test_permission_matrix_allow_deny_ask_and_owner_decisions(tmp_path):
    script = [
        ("permission", "Read", {"file_path": "a.py"}),
        ("permission", "Read", {"file_path": "/etc/passwd"}),
        ("permission", "Bash", {"command": "pytest -q"}),
        ("permission", "Bash", {"command": "pip install rich"}),
        ("permission", "Bash", {"command": "pip install click"}),
        ("permission", "Bash", {"command": "rm -rf build"}),
    ]
    host, events, prompts, clients = _host(tmp_path, script)
    await host.start()
    await host.send("go")
    await _wait_for(lambda: len(prompts) >= 1)
    first = prompts[0]
    assert first.tool_name == "Bash" and first.summary == "pip install rich" and first.allow_pattern == "pip install:*"
    assert host.resolve_permission(first.request_id, allow=True, allow_pattern="pip install:*")
    await _wait_for(lambda: len(prompts) >= 2)
    second = prompts[1]
    assert second.summary == "rm -rf build"
    assert host.resolve_permission(second.request_id, allow=False)
    assert not host.resolve_permission(second.request_id, allow=True)
    await _wait_for(lambda: any(e["kind"] == "result" for e in events))
    calls = clients[0].permission_calls
    assert calls[0] == ("Read", "Allow", None)
    assert calls[1][0:2] == ("Read", "Deny")
    assert calls[2] == ("Bash", "Allow", None)
    assert calls[3] == ("Bash", "Allow", None)
    assert calls[4] == ("Bash", "Allow", None)  # covered by the session pattern, no prompt
    assert calls[5][0:2] == ("Bash", "Deny") and "拒絕" in calls[5][2]
    assert len(prompts) == 2
    await host.close()


@pytest.mark.asyncio
async def test_permission_prompt_times_out_to_deny(tmp_path):
    host, events, prompts, clients = _host(tmp_path, [("permission", "Bash", {"command": "curl x"})], permission_timeout=0.05)
    await host.start()
    await host.send("go")
    await _wait_for(lambda: any(e["kind"] == "result" for e in events))
    assert clients[0].permission_calls[0][1] == "Deny" and "逾時" in clients[0].permission_calls[0][2]
    await host.close()


def test_write_mcp_files_keeps_the_token_private_and_next_to_the_workspace(tmp_path):
    import json
    import os
    import stat

    from dispatch_agent.sdk_adapter import write_mcp_files

    session_dir = tmp_path / "sessions" / "s1"
    config_path = write_mcp_files(session_dir, {"dispatch_base_url": "https://a.example/", "token": "dat_x.y", "max_calls": 5})
    assert config_path == session_dir / "tools.json"
    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "dispatch_base_url": "https://a.example",
        "token_file": "token",
        "calls_log": "tool_calls.jsonl",
        "max_calls": 5,
        "source": "assistant",
    }
    token_path = session_dir / "token"
    assert token_path.read_text(encoding="utf-8") == "dat_x.y\n"
    assert stat.S_IMODE(os.stat(token_path).st_mode) == 0o600
    assert not (session_dir / "repo" / "token").exists()
    with pytest.raises(ValueError):
        write_mcp_files(session_dir, {"dispatch_base_url": "ftp://x", "token": "t"})
    with pytest.raises(ValueError):
        write_mcp_files(session_dir, {"dispatch_base_url": "https://x", "token": "has space"})


@pytest.mark.asyncio
async def test_session_host_passes_mcp_config_to_the_options_factory(tmp_path):
    host, events, prompts, clients = _host(tmp_path, [AssistantMessage([TextBlock("hi")])])
    captured = {}

    def options_factory(*, cwd, can_use_tool, resume, mcp_config_path=None):
        captured["mcp_config_path"] = mcp_config_path
        return {"cwd": cwd, "can_use_tool": can_use_tool, "resume": resume}

    host._options_factory = options_factory
    await host.start(mcp={"dispatch_base_url": "https://a.example", "token": "dat_x.y"})
    assert captured["mcp_config_path"] == tmp_path / "tools.json"
    await host.close()
