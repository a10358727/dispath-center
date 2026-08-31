"""DG-AGENT-RUNTIME-V3 Phase 1b (R6): the job-backed coding-agent execution
channel is retired. These pins replace the retired execution suites
(`test_coding_task`, `test_claude_code_agent`, `test_codex_app_server`,
`test_engineering_command`): new requests are refused fail-closed with an
explicit pointer to Studio sessions, still-pending cards get an honest
rejection at decision time, kinds stay registered for historical rows, and
the provider registry truthfully reports every exec adapter as retired."""

from __future__ import annotations

import asyncio
import inspect

import pytest
from fastapi.testclient import TestClient

from app.approvals import (
    InvalidCodingTaskRequestError,
    InvalidEngineeringCommandRequestError,
    InvalidEngineeringTaskRequestError,
    approve,
    request_coding_task_approval,
    request_engineering_command_approval,
    request_engineering_task_approval,
)
from app.coding_agents import list_coding_agent_runtime_capability_snapshots, list_coding_agents
from app.db import VALID_APPROVAL_KINDS, Database

RETIREMENT_MARK = "已退役"


def _call_with_placeholder_args(fn):
    kwargs = {}
    for name, parameter in inspect.signature(fn).parameters.items():
        if parameter.default is inspect.Parameter.empty:
            kwargs[name] = None
    result = fn(**kwargs)
    if inspect.iscoroutine(result):
        asyncio.run(result)


@pytest.mark.parametrize(
    ("fn", "error"),
    [
        (request_coding_task_approval, InvalidCodingTaskRequestError),
        (request_engineering_task_approval, InvalidEngineeringTaskRequestError),
        (request_engineering_command_approval, InvalidEngineeringCommandRequestError),
    ],
)
def test_request_paths_are_retired_before_touching_anything(fn, error):
    with pytest.raises(error) as excinfo:
        _call_with_placeholder_args(fn)
    assert RETIREMENT_MARK in str(excinfo.value) and "Studio" in str(excinfo.value)


@pytest.mark.parametrize("kind", ["coding_task", "engineering_task_retry", "engineering_command"])
def test_pending_cards_get_an_honest_rejection_at_decision_time(tmp_path, kind):
    db = Database(str(tmp_path / "t.db"))
    approval_id = db.insert_approval(kind=kind, payload={"project": "proj1"})
    result = asyncio.run(
        approve(db, approval_id, audit_path=str(tmp_path / "audit.jsonl"))
    )
    decided = result["approval"]
    assert decided.status == "rejected"
    assert RETIREMENT_MARK in (decided.note or "")
    # the kinds stay registered: historical rows keep validating
    assert kind in VALID_APPROVAL_KINDS


def test_registry_reports_every_exec_adapter_as_retired():
    descriptors = list_coding_agents()
    assert [d.provider_id for d in descriptors] == ["claude-code", "codex"]
    snapshots = list_coding_agent_runtime_capability_snapshots()
    assert all(snapshot["retired"] is True for snapshot in snapshots)
    assert all(not any(snapshot["operations"].values()) for snapshot in snapshots)
    assert all("Studio" in snapshot["note"] for snapshot in snapshots)
    import app.coding_agents as coding_agents

    assert not hasattr(coding_agents, "get_coding_agent_provider")
    assert not hasattr(coding_agents, "require_coding_agent_provider")


def test_request_routes_refuse_with_the_retirement_reason(tmp_path, monkeypatch):
    servers_yaml = tmp_path / "servers.yaml"
    servers_yaml.write_text(
        "servers:\n  - name: server-a\n    host: 10.0.0.1\n    user: train\n    key: ~/.ssh/id_rsa\n    enabled: true\n"
    )
    for key, value in {
        "DB_PATH": str(tmp_path / "t.db"),
        "AUDIT_PATH": str(tmp_path / "audit.jsonl"),
        "SERVERS_YAML_PATH": str(servers_yaml),
        "SSH_KEY_ALLOWED_DIRS": str(tmp_path / ".ssh"),
        "API_V2_ENABLED": "true",
        "PRODUCT_RBAC_V2_ENABLED": "true",
        "ENGINEERING_TASK_BACKEND_V1": "true",
        "ENGINEERING_TASK_BACKEND_V1_ACCEPT_UNSANDBOXED_FINALIZATION": "true",
        "CODEX_RUNNER_SERVER": "server-a",
        "CODEX_WORKSPACE_ROOT": "~/codex_workspaces",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    import app.main as main_module

    async def isolated_monitor_loop(_self):
        import asyncio as _asyncio

        await _asyncio.Event().wait()

    monkeypatch.setattr(main_module.AppState, "monitor_loop", isolated_monitor_loop)

    async def healthy_runner_status(_self):
        return {
            "configured": True,
            "server": "server-a",
            "online": True,
            "available": True,
            "probe_status": "ok",
            "codex_installed": True,
            "authenticated": True,
            "reason": None,
        }

    # the route probes the runner before validating; the probe is not what this
    # test pins — the retirement refusal after it is
    monkeypatch.setattr(main_module.AppState, "get_codex_runner_status", healthy_runner_status)
    body = {
        "project_version_id": "whatever",
        "agent_provider_id": "codex",
        "objective": "x",
        "allowed_paths": ["app/"],
        "validation": {"tests_lint": False, "build_smoke": False},
        "execution_permissions": {
            "modify_project_files": True,
            "install_dependencies": False,
            "external_network": False,
        },
    }
    with TestClient(main_module.app) as client:
        db = main_module.app_state.db
        db.insert_project("proj1", "https://example.invalid/proj1.git")
        version = db.get_or_create_project_version("proj1", "a" * 40, git_ref="main")
        body["project_version_id"] = version.id
        legacy = client.post("/projects/proj1/engineering-tasks/request", json=body)
        assert legacy.status_code == 400 and RETIREMENT_MARK in legacy.text
        v2 = client.post("/api/v2/legacy-projects/proj1/engineering-task-requests", json=body)
        assert v2.status_code == 400 and RETIREMENT_MARK in v2.text
        assert db.list_approvals() == []
