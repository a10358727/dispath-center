"""DG-AGENT-RUNTIME-V3 R3 / INV-AGENT-1: runner-agent enrolment and revocation."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.audit import read_audit
from app.config import ServerConfig
from app.identity import hash_secret, parse_agent_runner_token, redact_token


@pytest.fixture
def runner_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.setenv("API_V2_ENABLED", "true")
    monkeypatch.setenv("PRODUCT_RBAC_V2_ENABLED", "true")
    monkeypatch.setenv("AGENT_RUNTIME_V3_ENABLED", "true")
    import app.main as main_module

    with TestClient(main_module.app) as client:
        state = main_module.app_state
        state.server_configs["worker-a"] = ServerConfig(name="worker-a", host="10.0.0.1", user="w", key="~/.ssh/k")
        yield client, state


def _approve(state, approval_id):
    from app.approvals import approve

    return asyncio.run(
        approve(state.db, approval_id, server_configs=state.server_configs, app_state=state, audit_path=state.config.audit_path)
    )


def test_enroll_request_is_a_pending_card_without_any_secret(runner_client):
    client, state = runner_client
    resp = client.post("/api/v2/agent-runners/enroll-requests", json={"server": "worker-a"})
    assert resp.status_code == 202, resp.text
    approval = resp.json()["approval"]
    assert approval["kind"] == "agent_runner_enroll" and approval["status"] == "pending"
    assert approval["payload"] == {"server": "worker-a"}
    assert "token" not in resp.text.lower() and "dar_" not in resp.text
    assert state.db.list_agent_runners() == []
    assert client.post("/api/v2/agent-runners/enroll-requests", json={"server": "ghost"}).status_code == 400
    assert client.get("/api/v2/agent-runners").json() == {"enabled": True, "agent_runners": []}


def test_enroll_approval_returns_the_credential_exactly_once_and_stores_only_its_digest(runner_client):
    client, state = runner_client
    approval_id = client.post("/api/v2/agent-runners/enroll-requests", json={"server": "worker-a"}).json()["approval"]["id"]
    result = _approve(state, approval_id)
    raw = result["agent_runner_token"]
    runner = result["agent_runner"]
    assert raw.startswith("dar_") and parse_agent_runner_token(raw)[0] == runner.id
    assert runner.is_active and runner.server_name == "worker-a"
    assert state.db.get_agent_runner_by_secret_hash(hash_secret(raw)).id == runner.id
    assert raw not in open(state.config.audit_path, encoding="utf-8").read()
    assert raw.split(".", 1)[1] not in open(state.config.audit_path, encoding="utf-8").read()
    assert redact_token(raw).endswith("<redacted>")
    listed = client.get("/api/v2/agent-runners").json()["agent_runners"]
    assert listed[0]["id"] == runner.id and listed[0]["connected"] is False and listed[0]["active"] is True
    assert "secret" not in str(listed[0]) and raw not in str(listed[0])
    # a second active runner for the same machine is refused at request time
    assert client.post("/api/v2/agent-runners/enroll-requests", json={"server": "worker-a"}).status_code == 400
    actions = [row["action"] for row in read_audit(state.config.audit_path)]
    assert "agent_runner_enroll" in actions


def test_enroll_approval_revalidates_and_rejects_stale_targets(runner_client):
    client, state = runner_client
    approval_id = client.post("/api/v2/agent-runners/enroll-requests", json={"server": "worker-a"}).json()["approval"]["id"]
    state.server_configs["worker-a"] = ServerConfig(name="worker-a", host="10.0.0.1", user="w", key="~/.ssh/k", enabled=False)
    result = _approve(state, approval_id)
    assert result["approval"].status == "rejected" and "agent_runner_token" not in result
    assert state.db.list_agent_runners() == []


def test_revoke_request_and_approval_retire_the_credential(runner_client):
    client, state = runner_client
    approval_id = client.post("/api/v2/agent-runners/enroll-requests", json={"server": "worker-a"}).json()["approval"]["id"]
    runner = _approve(state, approval_id)["agent_runner"]
    resp = client.post(f"/api/v2/agent-runners/{runner.id}/revoke-requests")
    assert resp.status_code == 202
    revoke_id = resp.json()["approval"]["id"]
    assert resp.json()["approval"]["payload"] == {"runner_id": runner.id, "server": "worker-a"}
    result = _approve(state, revoke_id)
    assert result["agent_runner"].status == "revoked" and result["agent_runner"].revoked_at
    assert not state.db.get_agent_runner(runner.id).is_active
    assert client.post(f"/api/v2/agent-runners/{runner.id}/revoke-requests").status_code == 400
    # the machine can be enrolled again after revocation
    assert client.post("/api/v2/agent-runners/enroll-requests", json={"server": "worker-a"}).status_code == 202


def test_runner_routes_are_hidden_when_the_flag_is_off(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "off.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.setenv("API_V2_ENABLED", "true")
    monkeypatch.setenv("PRODUCT_RBAC_V2_ENABLED", "true")
    monkeypatch.setenv("AGENT_RUNTIME_V3_ENABLED", "false")
    import app.main as main_module

    with TestClient(main_module.app) as client:
        state = main_module.app_state
        state.server_configs["worker-a"] = ServerConfig(name="worker-a", host="10.0.0.1", user="w", key="~/.ssh/k")
        resp = client.post("/api/v2/agent-runners/enroll-requests", json={"server": "worker-a"})
        assert resp.status_code == 404
        assert client.get("/api/v2/agent-runners").json()["enabled"] is False


def test_session_runtime_events_and_permission_rows_round_trip(runner_client):
    client, state = runner_client
    db = state.db
    session_id = _insert_session(db)
    db.upsert_agent_session_runtime(session_id, task_state="submitted")
    db.upsert_agent_session_runtime(session_id, task_state="working", cost_usd=0.5, last_seq=3)
    runtime = db.get_agent_session_runtime(session_id)
    assert runtime["task_state"] == "working" and runtime["cost_usd"] == 0.5 and runtime["last_seq"] == 3
    assert db.append_agent_session_event(session_id, 1, "assistant_text", {"text": "hi"})
    assert not db.append_agent_session_event(session_id, 1, "assistant_text", {"text": "dup"})
    assert db.append_agent_session_event(session_id, 2, "tool_use", {"name": "Bash", "input": {"command": "x" * 70000}})
    events = db.list_agent_session_events(session_id, after_seq=0)
    assert [e["seq"] for e in events] == [1, 2] and events[1]["payload"]["_truncated"] is True
    assert db.insert_agent_permission_request(request_id="req-00000001", session_id=session_id, tool_name="Bash", tool_input={"command": "pip install rich"}, summary="pip install rich", reason="outside allowlist", allow_pattern="pip install:*")
    assert db.list_agent_permission_requests(session_id)[0]["status"] == "pending"
    assert not db.insert_agent_permission_request(request_id="req-00000001", session_id=session_id, tool_name="Bash", tool_input={}, summary="dup", reason="r", allow_pattern=None)
    assert db.decide_agent_permission_request("req-00000001", status="allowed", decided_by_actor_id="actor", decision_allow_pattern="pip install:*")
    assert not db.decide_agent_permission_request("req-00000001", status="denied", decided_by_actor_id="actor")
    assert db.get_agent_permission_request("req-00000001")["status"] == "allowed"
    db.insert_agent_permission_request(request_id="req-00000002", session_id=session_id, tool_name="Bash", tool_input={}, summary="curl", reason="r", allow_pattern=None, now="2026-01-01T00:00:00+00:00")
    assert db.expire_agent_permission_requests(cutoff_iso="2026-06-01T00:00:00+00:00") == 1


def _insert_session(db):
    """Create the minimal project/conversation/session rows the FK chain needs."""
    import uuid

    from app.db import now_iso

    project_id = str(uuid.uuid4())
    conversation_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    with db.cursor() as cur:
        project_columns = [row["name"] for row in cur.execute("PRAGMA table_info(projects)").fetchall()]
        values = {"id": project_id, "name": "expdemo", "created_at": now_iso()}
        for column in project_columns:
            if column in values:
                continue
            notnull = next(r for r in cur.execute("PRAGMA table_info(projects)").fetchall() if r["name"] == column)["notnull"]
            if notnull:
                values[column] = "x" if column != "created_at" else now_iso()
        cur.execute(f"INSERT INTO projects ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})", tuple(values.values()))
        conv_columns = {row["name"]: row["notnull"] for row in cur.execute("PRAGMA table_info(ai_conversations)").fetchall()}
        conv = {"id": conversation_id, "project_id": project_id}
        for column, notnull in conv_columns.items():
            if column not in conv and notnull:
                conv[column] = now_iso() if column.endswith("_at") else "x"
        cur.execute(f"INSERT INTO ai_conversations ({', '.join(conv)}) VALUES ({', '.join('?' for _ in conv)})", tuple(conv.values()))
        sess_columns = {row["name"]: row["notnull"] for row in cur.execute("PRAGMA table_info(agent_sessions)").fetchall()}
        sess = {"id": session_id, "project_id": project_id, "conversation_id": conversation_id, "provider_id": "claude-agent-sdk", "workspace_branch": "session/x", "status": "active", "turn_count": 0, "max_turns": 200, "turn_timeout_sec": 600, "created_at": now_iso(), "last_used_at": now_iso()}
        for column, notnull in sess_columns.items():
            if column not in sess and notnull:
                sess[column] = "x"
        cur.execute(f"INSERT INTO agent_sessions ({', '.join(sess)}) VALUES ({', '.join('?' for _ in sess)})", tuple(sess.values()))
    return session_id
