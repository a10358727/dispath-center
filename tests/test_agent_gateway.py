"""DG-AGENT-RUNTIME-V3 gateway: runner WebSocket, persisted session events,
permission prompts and Studio routes, driven end to end through TestClient."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import ServerConfig
from app.identity import ActorType, generate_session_token
from app.db import now_iso
from dispatch_center import agent_protocol as protocol


@pytest.fixture
def studio_client(tmp_path, monkeypatch):
    servers_yaml = tmp_path / "servers.yaml"
    servers_yaml.write_text(
        "servers:\n  - name: server-a\n    host: 10.0.0.1\n    user: train\n    key: ~/.ssh/id_rsa\n    enabled: true\n"
    )
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("SERVERS_YAML_PATH", str(servers_yaml))
    monkeypatch.setenv("SSH_KEY_ALLOWED_DIRS", str(tmp_path / ".ssh"))
    # enforce mode needs a resolved actor: the compatible shared token acts as
    # the legacy platform admin on every HTTP call (see the WS envelope below)
    monkeypatch.setenv("AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("API_V2_ENABLED", "true")
    monkeypatch.setenv("PRODUCT_RBAC_V2_ENABLED", "true")
    # the pilot runs in enforce mode: every studio route must resolve its resource
    monkeypatch.setenv("AUTHORIZATION_MODE", "enforce")
    monkeypatch.setenv("AGENT_RUNTIME_V3_ENABLED", "true")
    monkeypatch.setenv("AGENT_SESSION_V1_ENABLED", "true")
    monkeypatch.setenv("CODEX_RUNNER_SERVER", "server-a")
    monkeypatch.setenv("CODEX_WORKSPACE_ROOT", "~/codex_workspaces")
    import app.main as main_module

    async def isolated_monitor_loop(_self):
        await asyncio.Event().wait()

    monkeypatch.setattr(main_module.AppState, "monitor_loop", isolated_monitor_loop)
    with TestClient(main_module.app) as client:
        state = main_module.app_state
        # HTTP calls act as a signed-in human platform admin (the pilot reality);
        # the shared token only serves the stream socket's first-message envelope.
        actor = state.db.insert_actor(actor_type=ActorType.HUMAN, display_name="Studio operator", platform_admin=True)
        issued = generate_session_token()
        state.db.insert_actor_session(session_id=issued.id, actor_id=actor.id, secret_hash=issued.secret_hash, expires_at="2099-01-01T00:00:00+00:00")
        client.cookies.set(state.config.session_cookie_name, issued.raw_token)
        state.server_configs["server-a"] = ServerConfig(name="server-a", host="10.0.0.1", user="train", key="~/.ssh/id_rsa")
        yield client, state


def _approve(state, approval_id):
    from app.approvals import approve

    return asyncio.run(approve(state.db, approval_id, server_configs=state.server_configs, app_state=state, audit_path=state.config.audit_path))


def _insert_rows(cur, table, values):
    columns = {row["name"]: row["notnull"] for row in cur.execute(f"PRAGMA table_info({table})").fetchall()}
    for column, notnull in columns.items():
        if column not in values and notnull:
            if column.endswith("_at"):
                values[column] = now_iso()
            elif column.endswith(("_paths", "_json", "_names")) or column in ("metadata", "tags"):
                values[column] = "[]"
            else:
                values[column] = "x"
    cur.execute(f"INSERT INTO {table} ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})", tuple(values.values()))


def _seed_project(state, *, name="expdemo", server="server-a"):
    project_id = str(uuid.uuid4())
    version_id = str(uuid.uuid4())
    with state.db.cursor() as cur:
        _insert_rows(cur, "projects", {"id": project_id, "name": name, "repo_or_path": "/home/train/expdemo", "created_at": now_iso()})
        _insert_rows(cur, "project_versions", {"id": version_id, "project_name": name, "project_id": project_id, "git_commit": "cf631a47" + "0" * 32, "created_at": now_iso()})
        _insert_rows(cur, "project_instances", {"id": "inst-1", "project_name": name, "project_id": project_id, "server": server, "path": "/home/train/expdemo", "state": "available", "dirty": 0, "last_seen": now_iso()})
    return name, version_id


def _enroll_runner(client, state, server="server-a"):
    approval_id = client.post("/api/v2/agent-runners/enroll-requests", json={"server": server}).json()["approval"]["id"]
    result = _approve(state, approval_id)
    return result["agent_runner"], result["agent_runner_token"]


def _open_session(client, state, project, version_id, runner_id):
    resp = client.post(f"/api/v2/studio/projects/{project}/sessions/open-requests", json={"base_version_id": version_id, "runner_id": runner_id})
    assert resp.status_code == 202, resp.text
    approval_id = resp.json()["approval"]["id"]
    result = _approve(state, approval_id)
    assert result["approval"].status == "approved", result["approval"].note
    return result["agent_session"]


def _frame(ws):
    return json.loads(ws.receive_text())


def test_runner_socket_rejects_bad_credentials_and_accepts_enrolled_runners(studio_client):
    client, state = studio_client
    runner, raw = _enroll_runner(client, state)
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/agent-runner/ws", headers={"X-Agent-Runner-Token": "dar_nope"}):
            pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/agent-runner/ws"):
            pass
    with client.websocket_connect("/agent-runner/ws", headers={"X-Agent-Runner-Token": raw}) as ws:
        ws.send_text(protocol.hello("server-a", "0.1.0", {"capabilities": {"toolchains": ["git"], "gpus": []}}))
        ack = _frame(ws)
        assert ack["method"] == protocol.M_HELLO_ACK and ack["params"]["runner_id"] == runner.id
        listed = client.get("/api/v2/agent-runners").json()["agent_runners"][0]
        assert listed["connected"] is True and listed["agent_card"]["capabilities"]["toolchains"] == ["git"]
    assert client.get("/api/v2/agent-runners").json()["agent_runners"][0]["connected"] is False


def test_session_round_trip_through_runner_and_studio_routes(studio_client):
    client, state = studio_client
    project, version_id = _seed_project(state)
    runner, raw = _enroll_runner(client, state)
    session = _open_session(client, state, project, version_id, runner.id)
    detail = client.get(f"/api/v2/studio/sessions/{session.id}").json()
    assert detail["provider_id"] == "claude-agent-sdk" and detail["runtime"]["runner_id"] == runner.id
    assert detail["runtime"]["task_state"] == "unknown" and detail["runtime"]["runner_connected"] is False
    assert client.post(f"/api/v2/studio/sessions/{session.id}/start").status_code == 409

    with client.websocket_connect("/agent-runner/ws", headers={"X-Agent-Runner-Token": raw}) as ws:
        ws.send_text(protocol.hello("server-a", "0.1.0", {}))
        _frame(ws)
        started = client.post(f"/api/v2/studio/sessions/{session.id}/start")
        assert started.status_code == 200, started.text
        assert started.json()["opened"] == {"project": project, "base_commit": "cf631a47" + "0" * 32, "source": "/home/train/expdemo"}
        opened = _frame(ws)
        assert opened["method"] == protocol.M_SESSION_OPEN and opened["params"]["session_id"] == session.id

        ws.send_text(protocol.session_status(session.id, "working", detail="ready"))
        assert client.post(f"/api/v2/studio/sessions/{session.id}/messages", json={"text": "把 README 加一段"}).status_code == 202
        message = _frame(ws)
        assert message["method"] == protocol.M_SESSION_MESSAGE and message["params"]["text"] == "把 README 加一段"

        ws.send_text(protocol.session_event(session.id, 1, {"kind": "assistant_text", "text": "好的"}))
        ws.send_text(protocol.session_event(session.id, 2, {"kind": "tool_use", "tool_use_id": "t1", "name": "Bash", "input": {"command": "pip install rich"}}))
        ws.send_text(protocol.permission_request(session.id, "req-00000001", "Bash", {"command": "pip install rich"}, "pip install rich", "outside allowlist", "pip install:*"))
        ws.send_text(protocol.session_status(session.id, "input-required", detail="pip install rich"))

        def events():
            return client.get(f"/api/v2/studio/sessions/{session.id}/events").json()["events"]

        for _ in range(50):
            if any(e["kind"] == "permission" for e in events()):
                break
            import time

            time.sleep(0.02)
        recorded = events()
        kinds = [e["kind"] for e in recorded]
        states = [e["payload"]["state"] for e in recorded if e["kind"] == "status"]
        assert states[0] == "unknown"  # the start attempt before the runner connected
        assert "submitted" in states and "working" in states and "input-required" in states
        assert kinds.index("user_text") > kinds.index("status")
        assert "assistant_text" in kinds and "tool_use" in kinds and "permission" in kinds
        assert [e["seq"] for e in recorded] == list(range(1, len(recorded) + 1))
        pending = client.get(f"/api/v2/studio/sessions/{session.id}").json()["pending_permissions"]
        assert pending and pending[0]["id"] == "req-00000001" and pending[0]["status"] == "pending"

        decided = client.post(f"/api/v2/studio/sessions/{session.id}/permissions/req-00000001/decision", json={"decision": "allow", "allow_pattern": "pip install:*"})
        assert decided.status_code == 200, decided.text
        decision = _frame(ws)
        assert decision["method"] == protocol.M_PERMISSION_DECISION
        assert decision["params"] == {"session_id": session.id, "request_id": "req-00000001", "decision": "allow", "allow_pattern": "pip install:*"}
        assert client.post(f"/api/v2/studio/sessions/{session.id}/permissions/req-00000001/decision", json={"decision": "deny"}).status_code == 409
        assert state.db.get_agent_permission_request("req-00000001")["status"] == "allowed"

        ws.send_text(protocol.session_event(session.id, 3, {"kind": "result", "is_error": False, "num_turns": 2, "total_cost_usd": 0.12, "sdk_session_id": "sdk-abc", "text": "done"}))
        ws.send_text(protocol.session_status(session.id, "completed"))
        for _ in range(50):
            runtime = client.get(f"/api/v2/studio/sessions/{session.id}").json()["runtime"]
            if runtime["task_state"] == "completed":
                break
            import time

            time.sleep(0.02)
        assert runtime["task_state"] == "completed" and runtime["cost_usd"] == 0.12 and runtime["sdk_session_id"] == "sdk-abc"

        # diff round trip: Studio asks, runner answers
        import threading

        holder = {}

        def ask():
            holder["resp"] = client.get(f"/api/v2/studio/sessions/{session.id}/diff")

        thread = threading.Thread(target=ask)
        thread.start()
        diff_req = _frame(ws)
        assert diff_req["method"] == protocol.M_SESSION_DIFF
        ws.send_text(protocol.notification(protocol.M_SESSION_DIFF_RESULT, {"session_id": session.id, "ok": True, "patch": "--- a\n+++ b\n", "status": [" M a.py"], "truncated": False, "head": "abc"}))
        thread.join(timeout=5)
        assert holder["resp"].status_code == 200 and holder["resp"].json()["patch"].startswith("--- a")

        closed = client.post(f"/api/v2/studio/sessions/{session.id}/close")
        assert closed.status_code == 200 and closed.json()["status"] == "closed"
        assert _frame(ws)["method"] == protocol.M_SESSION_CLOSE

    # audit never carries the runner credential
    audit_text = open(state.config.audit_path, encoding="utf-8").read()
    assert raw not in audit_text and "agent_permission_decided" in audit_text


def test_runner_disconnect_marks_open_sessions_unknown_not_failed(studio_client):
    client, state = studio_client
    project, version_id = _seed_project(state)
    runner, raw = _enroll_runner(client, state)
    session = _open_session(client, state, project, version_id, runner.id)
    with client.websocket_connect("/agent-runner/ws", headers={"X-Agent-Runner-Token": raw}) as ws:
        ws.send_text(protocol.hello("server-a", "0.1.0", {}))
        _frame(ws)
        assert client.post(f"/api/v2/studio/sessions/{session.id}/start").status_code == 200
        _frame(ws)
        # a frame about a session this runner does not host is dropped
        ws.send_text(protocol.session_status("other-session", "failed"))
    for _ in range(50):
        state_now = client.get(f"/api/v2/studio/sessions/{session.id}").json()["runtime"]["task_state"]
        if state_now == "unknown":
            break
        import time

        time.sleep(0.02)
    assert state_now == "unknown"
    assert client.post(f"/api/v2/studio/sessions/{session.id}/messages", json={"text": "hi"}).status_code == 409


def test_studio_stream_replays_persisted_events(studio_client):
    client, state = studio_client
    project, version_id = _seed_project(state)
    runner, raw = _enroll_runner(client, state)
    session = _open_session(client, state, project, version_id, runner.id)
    gateway = __import__("app.agent_gateway", fromlist=["ensure_agent_gateway"]).ensure_agent_gateway(state)
    asyncio.run(gateway._record(session.id, "assistant_text", {"kind": "assistant_text", "text": "persisted"}))
    with client.websocket_connect(f"/api/v2/studio/sessions/{session.id}/stream") as ws:
        # browsers send the session cookie; tests use the first-message envelope
        ws.send_json({"type": "auth", "token": "secret-token"})
        first = ws.receive_json()
        assert first["kind"] == "assistant_text" and first["payload"]["text"] == "persisted" and first["seq"] == 1


def test_studio_routes_are_hidden_when_flag_off(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "off.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.setenv("API_V2_ENABLED", "true")
    monkeypatch.setenv("PRODUCT_RBAC_V2_ENABLED", "true")
    monkeypatch.setenv("AGENT_RUNTIME_V3_ENABLED", "false")
    import app.main as main_module

    with TestClient(main_module.app) as client:
        assert client.get("/api/v2/studio/sessions/nope").status_code == 404
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/agent-runner/ws", headers={"X-Agent-Runner-Token": "dar_x"}):
                pass


def test_start_issues_a_session_scoped_platform_token_for_the_speaking_actor(tmp_path, monkeypatch):
    servers_yaml = tmp_path / "servers.yaml"
    servers_yaml.write_text("servers:\n  - name: server-a\n    host: 10.0.0.1\n    user: train\n    key: ~/.ssh/id_rsa\n    enabled: true\n")
    for key, value in {
        "DB_PATH": str(tmp_path / "tok.db"), "AUDIT_PATH": str(tmp_path / "audit.jsonl"), "SERVERS_YAML_PATH": str(servers_yaml),
        "SSH_KEY_ALLOWED_DIRS": str(tmp_path / ".ssh"), "AUTH_TOKEN": "secret-token", "API_V2_ENABLED": "true", "PRODUCT_RBAC_V2_ENABLED": "true",
        "AGENT_RUNTIME_V3_ENABLED": "true", "AGENT_SESSION_V1_ENABLED": "true", "CODEX_RUNNER_SERVER": "server-a",
        "CODEX_WORKSPACE_ROOT": "~/codex_workspaces", "ASSISTANT_TOOLS_V1_ENABLED": "true", "ASSISTANT_TOOLS_DISPATCH_BASE_URL": "https://a.example",
    }.items():
        monkeypatch.setenv(key, value)
    import app.main as main_module

    async def isolated_monitor_loop(_self):
        await asyncio.Event().wait()

    monkeypatch.setattr(main_module.AppState, "monitor_loop", isolated_monitor_loop)
    headers = {"X-Auth-Token": "secret-token"}
    with TestClient(main_module.app) as client:
        state = main_module.app_state
        state.server_configs["server-a"] = ServerConfig(name="server-a", host="10.0.0.1", user="train", key="~/.ssh/id_rsa")
        project, version_id = _seed_project(state)
        approval_id = client.post("/api/v2/agent-runners/enroll-requests", json={"server": "server-a"}, headers=headers).json()["approval"]["id"]
        enrolled = _approve(state, approval_id)
        runner, raw = enrolled["agent_runner"], enrolled["agent_runner_token"]
        resp = client.post(f"/api/v2/studio/projects/{project}/sessions/open-requests", json={"base_version_id": version_id, "runner_id": runner.id}, headers=headers)
        session = _approve(state, resp.json()["approval"]["id"])["agent_session"]
        with client.websocket_connect("/agent-runner/ws", headers={"X-Agent-Runner-Token": raw}) as ws:
            ws.send_text(protocol.hello("server-a", "0.1.0", {}))
            _frame(ws)
            started = client.post(f"/api/v2/studio/sessions/{session.id}/start", headers=headers)
            assert started.status_code == 200, started.text
            assert "token" not in started.json()["opened"]["mcp"]
            opened = _frame(ws)["params"]
            token = opened["mcp"]["token"]
            assert token.startswith("dat_") and opened["mcp"]["dispatch_base_url"] == "https://a.example"
            rows = state.db._conn.execute("SELECT actor_id, revoked_at, turn_ref FROM assistant_turn_tokens").fetchall()
            assert len(rows) == 1 and rows[0]["actor_id"] == "00000000-0000-0000-0000-000000000001" and rows[0]["turn_ref"] == f"session:{session.id}"
            # the token works on an MCP-mapped route and nowhere else
            assert client.get("/servers", headers={"X-Auth-Token": token}).status_code == 200
            assert client.get("/auth/me", headers={"X-Auth-Token": token}).status_code == 403
            assert client.post(f"/api/v2/studio/sessions/{session.id}/close", headers=headers).status_code == 200
            assert state.db._conn.execute("SELECT revoked_at FROM assistant_turn_tokens").fetchone()["revoked_at"] is not None
            assert client.get("/servers", headers={"X-Auth-Token": token}).status_code == 401
        assert token not in open(state.config.audit_path, encoding="utf-8").read()


def test_session_options_are_pinned_into_the_card_shipped_to_the_runner_and_live_configurable(studio_client):
    client, state = studio_client
    project, version_id = _seed_project(state)
    runner, raw = _enroll_runner(client, state)
    # INV-AGENT-2: a prompt-silencing mode is rejected at request time (no card is created)
    denied = client.post(
        f"/api/v2/studio/projects/{project}/sessions/open-requests",
        json={"base_version_id": version_id, "runner_id": runner.id, "options": {"permission_mode": "bypassPermissions"}},
    )
    assert denied.status_code == 400 and "INV-AGENT-2" in denied.text
    resp = client.post(
        f"/api/v2/studio/projects/{project}/sessions/open-requests",
        json={"base_version_id": version_id, "runner_id": runner.id, "options": {"model": "sonnet", "effort": "high", "thinking": {"budget_tokens": 8000}, "permission_mode": "plan"}},
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["approval"]["payload"]["options"] == {"model": "sonnet", "effort": "high", "thinking": {"budget_tokens": 8000}, "permission_mode": "plan"}
    session = _approve(state, resp.json()["approval"]["id"])["agent_session"]
    assert state.db.get_agent_session_runtime(session.id)["options"]["permission_mode"] == "plan"
    with client.websocket_connect("/agent-runner/ws", headers={"X-Agent-Runner-Token": raw}) as ws:
        ws.send_text(protocol.hello("server-a", "0.1.0", {}))
        _frame(ws)
        assert client.post(f"/api/v2/studio/sessions/{session.id}/start").status_code == 200
        opened = _frame(ws)["params"]
        assert opened["options"] == {"model": "sonnet", "effort": "high", "thinking": {"budget_tokens": 8000}, "permission_mode": "plan"}
        changed = client.post(f"/api/v2/studio/sessions/{session.id}/configure", json={"model": "opus", "permission_mode": "acceptEdits"})
        assert changed.status_code == 200, changed.text
        assert changed.json()["options"] == {"model": "opus", "effort": "high", "thinking": {"budget_tokens": 8000}, "permission_mode": "acceptEdits"}
        frame = _frame(ws)
        assert frame["method"] == "session/configure" and frame["params"] == {"session_id": session.id, "model": "opus", "permission_mode": "acceptEdits"}
        refused = client.post(f"/api/v2/studio/sessions/{session.id}/configure", json={"permission_mode": "dontAsk"})
        assert refused.status_code == 400
        assert client.post(f"/api/v2/studio/sessions/{session.id}/configure", json={}).status_code == 400
        detail = client.get(f"/api/v2/studio/sessions/{session.id}").json()
        assert detail["runtime"]["options"]["model"] == "opus"
        kinds = [e["kind"] for e in client.get(f"/api/v2/studio/sessions/{session.id}/events").json()["events"]]
        assert "config" in kinds


def test_messages_relay_image_attachments_but_persist_only_metadata(studio_client):
    client, state = studio_client
    project, version_id = _seed_project(state)
    runner, raw = _enroll_runner(client, state)
    session = _open_session(client, state, project, version_id, runner.id)
    with client.websocket_connect("/agent-runner/ws", headers={"X-Agent-Runner-Token": raw}) as ws:
        ws.send_text(protocol.hello("server-a", "0.1.0", {}))
        _frame(ws)
        assert client.post(f"/api/v2/studio/sessions/{session.id}/start").status_code == 200
        _frame(ws)
        ok = client.post(
            f"/api/v2/studio/sessions/{session.id}/messages",
            json={"text": "看這張圖", "attachments": [{"type": "image", "media_type": "image/png", "data_base64": "aGVsbG8="}]},
        )
        assert ok.status_code == 202, ok.text
        frame = _frame(ws)
        assert frame["method"] == "session/message"
        assert frame["params"]["attachments"] == [{"type": "image", "media_type": "image/png", "data_base64": "aGVsbG8="}]
        events = client.get(f"/api/v2/studio/sessions/{session.id}/events").json()["events"]
        user = next(e for e in events if e["kind"] == "user_text")
        assert user["payload"]["attachments"] == [{"type": "image", "media_type": "image/png", "bytes": 5}]
        assert "data_base64" not in str(user["payload"])
        bad = client.post(
            f"/api/v2/studio/sessions/{session.id}/messages",
            json={"text": "x", "attachments": [{"type": "image", "media_type": "image/bmp", "data_base64": "aGk="}]},
        )
        assert bad.status_code == 400
        # files round trip for @-autocomplete
        import threading

        def answer():
            frame2 = _frame(ws)
            assert frame2["method"] == "session/files"
            ws.send_text(protocol.notification("session/files/result", {"session_id": session.id, "ok": True, "files": ["a.py", "docs/b.md"]}))

        thread = threading.Thread(target=answer)
        thread.start()
        listed = client.get(f"/api/v2/studio/sessions/{session.id}/files")
        thread.join(timeout=5)
        assert listed.status_code == 200 and listed.json()["files"] == ["a.py", "docs/b.md"]
