"""Workspace argv builders, ensure_workspace with a fake git, RunnerClient dispatch with a fake socket."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from dispatch_agent import protocol
from dispatch_agent.card import probe_agent_card
from dispatch_agent.client import RunnerClient
from dispatch_agent.config import AgentConfig
from dispatch_agent.workspace import (
    WorkspaceError,
    collect_diff,
    ensure_workspace,
    mirror_clone_argv,
    session_paths,
    worktree_add_argv,
)


def test_workspace_paths_and_argv_are_validated(tmp_path):
    paths = session_paths(tmp_path, "expdemo", "11111111-2222-4333-8444-555555555555")
    assert paths.mirror == tmp_path / "mirrors" / "expdemo.git"
    assert paths.repo == tmp_path / "sessions" / "11111111-2222-4333-8444-555555555555" / "repo"
    assert paths.branch == "session/11111111"
    assert mirror_clone_argv("/home/x/proj", paths.mirror)[:3] == ["git", "clone", "--mirror"]
    with pytest.raises(WorkspaceError):
        mirror_clone_argv("--upload-pack=evil", paths.mirror)
    with pytest.raises(WorkspaceError):
        worktree_add_argv(paths, "not-a-commit")
    with pytest.raises(WorkspaceError):
        session_paths(tmp_path, "bad name", "s")


def test_ensure_workspace_runs_argv_lists_without_a_shell(tmp_path):
    calls: list[list[str]] = []

    def run(argv):
        calls.append(argv)
        if argv[:3] == ["git", "clone", "--mirror"]:
            Path(argv[-1]).mkdir(parents=True)
        return subprocess.CompletedProcess(argv, 0, "", "")

    paths = ensure_workspace(root=tmp_path, project="expdemo", session_id="11111111-2222-4333-8444-555555555555", base_commit="cf631a47", source="/home/r/expdemo", bundle=None, run=run)
    assert [c[:3] for c in calls] == [["git", "clone", "--mirror"], ["git", f"--git-dir={paths.mirror}", "cat-file"], ["git", f"--git-dir={paths.mirror}", "worktree"]]
    assert all(isinstance(c, list) for c in calls)

    def failing(argv):
        if "cat-file" in argv:
            return subprocess.CompletedProcess(argv, 128, "", "fatal: nope")
        Path(argv[-1]).mkdir(parents=True, exist_ok=True) if argv[1] == "init" else None
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(WorkspaceError, match="base commit not in mirror"):
        ensure_workspace(root=tmp_path / "other", project="expdemo", session_id="22222222-2222-4333-8444-555555555555", base_commit="cf631a47", source=None, bundle=None, run=failing)


def test_collect_diff_is_bounded(tmp_path):
    def run(argv):
        if "diff" in argv:
            return subprocess.CompletedProcess(argv, 0, "x" * 300_000, "")
        if "status" in argv:
            return subprocess.CompletedProcess(argv, 0, " M a.py\n?? b.py\n", "")
        return subprocess.CompletedProcess(argv, 0, "abc123\n", "")

    result = collect_diff(tmp_path, run=run)
    assert result["truncated"] is True and len(result["patch"]) <= 256 * 1024
    assert result["status"] == [" M a.py", "?? b.py"] and result["head"] == "abc123" and result["ok"]


def test_agent_card_uses_closed_probes():
    def which(name):
        return "/usr/bin/" + name if name in ("python3", "git", "nvidia-smi", "openocd") else None

    def run(argv, timeout):
        assert argv[0] == "nvidia-smi"
        return subprocess.CompletedProcess(argv, 0, "NVIDIA GeForce RTX 5090, 32607\n", "")

    card = probe_agent_card(runner_name="worker_5090_106", version="0.1.0", which=which, run=run)
    assert card["capabilities"]["toolchains"] == ["git", "nvidia-smi", "openocd", "python3"]
    assert card["capabilities"]["gpus"] == [{"name": "NVIDIA GeForce RTX 5090", "memory_mb": 32607}]
    assert card["capabilities"]["devices"] == []


class FakeSocket:
    def __init__(self, incoming: list[str]):
        self.incoming = incoming
        self.sent: list[str] = []
        self.closed = False

    async def send(self, frame):
        self.sent.append(frame)

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.incoming:
            raise StopAsyncIteration
        return self.incoming.pop(0)


class FakeHost:
    def __init__(self, **kw):
        self.kw = kw
        self.workspace = kw["workspace"]
        self.sent: list[str] = []
        self.closed = False
        self.resolved: list = []
        self.started_with = None

    async def start(self, *, resume=None, mcp=None, options=None, workspace_context=None):
        self.mcp = mcp
        self.options = options
        self.workspace_context = workspace_context
        self.started_with = resume

    async def send(self, text):
        self.sent.append(text)
        await self.kw["on_event"]({"kind": "assistant_text", "text": "ok", "seq": 1})
        await self.kw["on_event"]({"kind": "result", "is_error": False, "seq": 2})

    async def interrupt(self):
        pass

    async def close(self):
        self.closed = True

    def resolve_permission(self, request_id, *, allow, allow_pattern=None):
        self.resolved.append((request_id, allow, allow_pattern))
        return True


def _config(tmp_path):
    return AgentConfig(server_url="https://a.example", runner_name="r1", workspace_root=tmp_path / "ws", credential="dar_11111111-2222-4333-8444-555555555555.abcdefghijklmnopqrstuvwxyz012345", validation_allowlist=("pytest",), heartbeat_sec=5.0)


@pytest.mark.asyncio
async def test_runner_client_opens_session_relays_message_and_permission_decision(tmp_path, monkeypatch):
    hosts: list[FakeHost] = []

    def host_factory(**kw):
        host = FakeHost(**kw)
        hosts.append(host)
        return host

    calls: list[list[str]] = []

    def fake_git(argv, **kw):
        calls.append(argv)
        if argv[:3] == ["git", "clone", "--mirror"]:
            Path(argv[-1]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(argv, 0, "abc\n", "")

    import dispatch_agent.workspace as ws_mod

    monkeypatch.setattr(ws_mod, "run_git", fake_git)
    incoming = [
        protocol.notification(protocol.M_HELLO_ACK, {}),
        protocol.notification(protocol.M_SESSION_OPEN, {"session_id": "aaaaaaaa-1111-4222-8333-444444444444", "project": "expdemo", "base_commit": "cf631a47", "source": "/home/r/expdemo"}),
        protocol.notification(protocol.M_SESSION_MESSAGE, {"session_id": "aaaaaaaa-1111-4222-8333-444444444444", "text": "hello"}),
        protocol.notification(protocol.M_PERMISSION_DECISION, {"session_id": "aaaaaaaa-1111-4222-8333-444444444444", "request_id": "req1", "decision": "allow", "allow_pattern": "pip install:*"}),
        protocol.notification(protocol.M_SESSION_MESSAGE, {"session_id": "nope", "text": "x"}),
        protocol.notification(protocol.M_SESSION_CLOSE, {"session_id": "aaaaaaaa-1111-4222-8333-444444444444"}),
        "garbage frame",
    ]
    socket = FakeSocket(incoming)
    headers_seen: dict = {}

    async def connect(url, headers):
        headers_seen.update({"url": url, **headers})
        return socket

    sleeps: list[float] = []

    async def sleep(seconds):
        # A fake sleep must still yield to the event loop, or the heartbeat
        # task spins forever without letting the receive loop run.
        sleeps.append(seconds)
        await asyncio.sleep(0.001)
        if len(sleeps) > 8:
            client._stopping = True

    client = RunnerClient(_config(tmp_path), agent_card={"capabilities": {}}, connect=connect, session_host_factory=host_factory, sleep=sleep)
    await asyncio.wait_for(client.run_forever(), timeout=3)

    assert headers_seen["url"] == "wss://a.example/agent-runner/ws"
    assert headers_seen["X-Agent-Runner-Token"].startswith("dar_")
    sent = [json.loads(f) for f in socket.sent]
    methods = [f["method"] for f in sent]
    assert methods[0] == protocol.M_HELLO
    states = [f["params"]["state"] for f in sent if f["method"] == protocol.M_SESSION_STATUS and f["params"]["session_id"] == "aaaaaaaa-1111-4222-8333-444444444444"]
    assert states[:3] == ["submitted", "working", "working"]  # submitted -> ready -> message
    assert "completed" in states
    events = [f["params"]["event"]["kind"] for f in sent if f["method"] == protocol.M_SESSION_EVENT]
    assert events == ["assistant_text", "result"]
    assert hosts[0].sent == ["hello"] and hosts[0].closed and hosts[0].resolved == [("req1", True, "pip install:*")]
    unknown = [f for f in sent if f["method"] == protocol.M_SESSION_STATUS and f["params"]["session_id"] == "nope"]
    assert unknown and unknown[0]["params"]["state"] == "failed"
    # any workspace git invocation is an argv list, never a shell string
    assert calls and all(isinstance(c, list) for c in calls)
    assert client.sessions == {}


@pytest.mark.asyncio
async def test_runner_client_reports_workspace_failure_without_starting_sdk(tmp_path, monkeypatch):
    import dispatch_agent.workspace as ws_mod

    monkeypatch.setattr(ws_mod, "run_git", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "boom"))
    hosts: list[Any] = []
    socket = FakeSocket([protocol.notification(protocol.M_SESSION_OPEN, {"session_id": "bbbbbbbb-1111-4222-8333-444444444444", "project": "p", "base_commit": "cf631a47", "source": "/x"})])

    async def connect(url, headers):
        return socket

    async def sleep(seconds):
        await asyncio.sleep(0.001)
        client._stopping = True

    client = RunnerClient(_config(tmp_path), agent_card={}, connect=connect, session_host_factory=lambda **kw: hosts.append(kw), sleep=sleep)
    await asyncio.wait_for(client.run_forever(), timeout=3)
    statuses = [json.loads(f)["params"] for f in socket.sent if json.loads(f)["method"] == protocol.M_SESSION_STATUS]
    assert statuses[-1]["state"] == "failed" and "workspace" in statuses[-1]["detail"]
    assert hosts == []


def test_cli_check_reports_problems_without_network(tmp_path, capsys, monkeypatch):
    import dispatch_agent.__main__ as cli

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli, "probe_agent_card", lambda **kw: {"capabilities": {"toolchains": ["git"], "gpus": []}})
    main = cli.main
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "config.json").write_text(json.dumps({"server_url": "https://a.example", "runner_name": "r1", "workspace_root": str(tmp_path / "ws")}), encoding="utf-8")
    (cfg / "agent.env").write_text("DISPATCH_AGENT_CREDENTIAL=dar_11111111-2222-4333-8444-555555555555.abcdefghijklmnopqrstuvwxyz012345\n", encoding="utf-8")
    (cfg / "agent.env").chmod(0o600)
    code = main(["--check", "--config-dir", str(cfg)])
    out = capsys.readouterr().out
    assert "ok  config: runner=r1" in out and "claude token: MISSING" in out
    assert code == 1


def test_workspace_plugin_copies_skills_and_commands_but_never_hooks_or_tool_grants(tmp_path):
    from dispatch_agent.workspace import build_session_context, build_workspace_plugin, read_project_instructions

    repo = tmp_path / "repo"
    session_dir = tmp_path / "session"
    (repo / ".claude" / "skills" / "deploy").mkdir(parents=True)
    (repo / ".claude" / "skills" / "deploy" / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: d\nallowed-tools:\n  - Bash(*)\nhooks:\n  PreToolUse: x\n---\n\nSteps here\n", encoding="utf-8")
    (repo / ".claude" / "skills" / "deploy" / "references" ).mkdir()
    (repo / ".claude" / "skills" / "deploy" / "references" / "notes.md").write_text("ref", encoding="utf-8")
    (repo / ".claude" / "commands").mkdir()
    (repo / ".claude" / "commands" / "ship.md").write_text("---\nallowed-tools: Bash(git:*)\n---\n!`git push --force`\nShip $ARGUMENTS\n", encoding="utf-8")
    (repo / ".claude" / "hooks").mkdir()
    (repo / ".claude" / "hooks" / "hooks.json").write_text("{}", encoding="utf-8")
    (repo / ".claude" / "settings.json").write_text('{"hooks": {}}', encoding="utf-8")
    (repo / ".mcp.json").write_text("{}", encoding="utf-8")
    (repo / "CLAUDE.md").write_text("Follow the project rules.", encoding="utf-8")
    (repo / ".claude" / "skills" / "evil-link").symlink_to("/etc")

    plugin = build_workspace_plugin(repo, session_dir)
    assert plugin == session_dir / "workspace-plugin"
    skill = (plugin / "skills" / "deploy" / "SKILL.md").read_text(encoding="utf-8")
    assert "Steps here" in skill and "allowed-tools" not in skill and "hooks" not in skill and "Bash(*)" not in skill
    assert (plugin / "skills" / "deploy" / "references" / "notes.md").is_file()
    command = (plugin / "commands" / "ship.md").read_text(encoding="utf-8")
    assert "Ship $ARGUMENTS" in command and "git push --force" not in command and "allowed-tools" not in command
    listed = {str(path.relative_to(plugin)) for path in plugin.rglob("*") if path.is_file()}
    assert not any("hooks" in item or item.endswith(".mcp.json") or "settings" in item for item in listed)
    assert (plugin / ".claude-plugin" / "plugin.json").is_file()

    context = build_session_context(repo, session_dir)
    assert context["plugin_dir"] == str(plugin)
    assert "Follow the project rules." in context["system_prompt_append"]
    assert read_project_instructions(tmp_path / "empty") == ""
    assert build_workspace_plugin(tmp_path / "empty", session_dir) is None
    assert build_session_context(tmp_path / "empty", session_dir) == {}
