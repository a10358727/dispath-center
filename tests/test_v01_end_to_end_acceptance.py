"""WP12 deterministic V0.1 acceptance journey.

This module is deliberately a composed scenario, not a test-discovery check.  It
drives the persisted Product/AgentSession/Run/approval/attempt/evidence seams in
one TestClient database.  Browser-only interactions are identified explicitly
in ``WP12_STEP_PROOF`` and remain covered by their direct Studio component tests.

The custom-port target and SSH work are offline fakes.  They prove the rental
target contract and closed SSH/tmux command shape; they do not claim contact
with an Internet rental host or resolve DG-SSH-HOSTKEY.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from app.config import ServerConfig
from app.execution_dispatch import collect_attempt, dispatch_job_via_attempt
from app.metrics_v1 import MetricsEntry
from app.server_publication import (
    SERVER_CONFIG_CONTRACT_VERSION,
    build_server_config_contract,
    publish_approved_server_mutation,
)
from tests.test_agent_session_run_v2 import _agent_session
from tests.test_execution_attempt_dispatch import ScriptedSSH
from tests.test_execution_plan_v2_api import (
    _enable_execution_plan_v2,
    _preview_request,
    _seed_execution_context,
)
from tests.test_product_runs_v2 import _running_product_run, _terminalize_attempt
from tests.test_run_templates_v2 import OPERATOR_ID, REVIEWER_ID, _session_for
from tests.test_mcp_bridge import _call_tool, _tool_text, make_config


# Every numbered WP12 scenario step has concrete proof. ``journey`` entries are
# asserted below against live persisted/API state; ``direct`` and ``studio``
# entries name the exact domain or browser suite composed with this harness.
WP12_STEP_PROOF = {
    1: ("studio", "overview.test.tsx: Project and Compute projections"),
    2: ("journey", "custom-port active ServerConfig revision"),
    3: ("journey", "eligible preflight plus ready observation"),
    4: ("journey", "persisted Project and AgentSession"),
    5: ("studio", "SessionView.test.tsx: Context usage or explicit unavailable"),
    6: ("direct", "apply_patch.py: bounded governed code/config change"),
    7: ("direct", "engineering_validation.py: governed validation lifecycle"),
    8: ("direct", "agent_session_checkpoint.py: governed review and promotion"),
    9: ("journey", "AgentSession run request creates typed pending plan"),
    10: ("journey", "human approval decision materializes queued Job"),
    11: ("journey", "attempt dispatcher emits closed SSH/tmux launch contract"),
    12: ("journey", "Product Run and bounded log projection"),
    13: ("journey", "attempt artifact metadata collection"),
    14: ("journey", "parsed metrics collection projection"),
    15: ("journey", "WP9 MCP tools read this exact persisted Run evidence"),
    16: ("studio", "transcript.test.ts: grounded evidence/recommendation boundary"),
    17: ("studio", "SessionView.test.tsx: human Continue action"),
    18: ("studio", "SessionView.test.tsx: Continue uses the same session action"),
}

WP12_LEAK_PROOF = {
    "api_and_audit": "journey payload/audit assertions below",
    "prompt": "test_dispatch_agent_session_host.py secret filtering assertions",
    "transcript": "test_agent_gateway.py persisted event/credential assertions",
    "ui": "SessionView.test.tsx renders bounded structured evidence only",
}


def _mcp_against_test_client(client, tool: str, arguments: dict) -> dict:
    async def handler(request: httpx.Request) -> httpx.Response:
        response = client.request(
            request.method,
            request.url.raw_path.decode(),
            headers={
                key: value for key, value in request.headers.items()
                if key not in {"host", "authorization"}
            },
            content=request.content,
        )
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers=dict(response.headers),
            request=request,
        )

    return json.loads(_tool_text(asyncio.run(_call_tool(make_config(), handler, tool, arguments))))


def test_wp12_composed_governed_journey_and_evidence(api_client):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    database = main_module.app_state.db

    # Steps 2-4, 8: the normal seed builds an approved/persisted ProjectVersion,
    # an active target revision, readiness evidence and an available instance.
    seed = _seed_execution_context(main_module)
    rental = {
        "name": "offline-rental", "host": "203.0.113.12", "port": 22022,
        "user": "worker", "key": "/tmp/offline-rental-key", "enabled": True,
        "execution_backend": "ssh", "project_roots": ["/srv/projects"],
        "dataset_roots": ["/srv/datasets"], "tags": ["gpu"],
    }
    rental_contract = build_server_config_contract(
        operation="add", server_name=rental["name"], yaml_before={"servers": []},
        yaml_after={"servers": [rental]}, server_payload=rental,
    )
    rental_approval = database.insert_pinned_approval(
        kind="server_add", contract_version=SERVER_CONFIG_CONTRACT_VERSION,
        payload=rental_contract,
    )
    published = publish_approved_server_mutation(
        database, approval_id=rental_approval, operation="add",
        server_name=rental["name"], server_payload=rental,
        yaml_before={"servers": []}, yaml_after={"servers": [rental]},
        decision_actor_id=REVIEWER_ID, write_yaml=lambda: None,
    )
    assert published.state == "activated"
    rental_revision = database.get_active_server_config_revision("offline-rental")
    assert '"port":22022' in rental_revision["normalized_target_json"]
    database.insert_server_observation(
        server_name="offline-rental", online=True, probe_ok=True,
        gpu_count=1, gpu_mem_used_mb=0, gpu_mem_total_mb=24576,
        mem_total_bytes=64 * 1024 * 1024 * 1024,
        mem_available_bytes=32 * 1024 * 1024 * 1024,
        disk_avail_bytes=512 * 1024 * 1024 * 1024,
    )
    assert database.list_server_observations(
        "offline-rental", since_iso="2000-01-01T00:00:00Z", limit=1
    )[0].probe_ok is True
    rental_revision = database.record_server_attempt_backend_preflight(
        server_name="offline-rental",
        revision_id=rental_revision["id"],
        status="eligible",
        contract_version="attempt-fs-preflight-v1",
        filesystem_type="ext4",
    )
    main_module.app_state.server_configs["offline-rental"] = ServerConfig(
        name="offline-rental", host="203.0.113.12", port=22022,
        user="worker", key="/tmp/offline-rental-key", enabled=True,
        execution_backend="ssh", project_roots=["/srv/projects"],
        dataset_roots=["/srv/datasets"], tags=["gpu"],
    )
    rental_instance = database.insert_project_instance(
        project_name=seed["project_name"],
        server="offline-rental",
        path="/srv/projects/product-v2-rental",
        git_branch="main",
        git_commit=seed["version"]["git_commit"],
        dirty=False,
    )
    database.update_instance_reconcile(
        rental_instance,
        state="available",
        git_branch="main",
        git_commit=seed["version"]["git_commit"],
        dirty=False,
        touch_last_seen=True,
    )
    with database.cursor() as cursor:
        assert cursor.execute(
            "SELECT 1 FROM projects WHERE id = ?", (seed["project_id"],)
        ).fetchone() is not None
    assert seed["version"]["git_commit"] == "a" * 40

    agent_session = _agent_session(database, seed)
    assert agent_session.project_id == seed["project_id"]
    assert agent_session.base_version_id == seed["version"]["id"]

    # Steps 9-10: typed Agent request_run is pending until a different human
    # explicitly approves it.  There is no Job before that decision.
    _session_for(client, main_module, OPERATOR_ID)
    request = _preview_request(seed)
    request["target_selection"]["server_config_revision_id"] = rental_revision["id"]
    preview = client.post(
        f"/api/v2/agent-sessions/{agent_session.id}/run-previews", json=request
    )
    assert preview.status_code == 200 and preview.json()["ready"] is True, (
        preview.json(), client.get(f"/api/v2/projects/{seed['project_id']}/workspace").json()
    )
    submitted = client.post(
        f"/api/v2/agent-sessions/{agent_session.id}/run-requests",
        headers={"Idempotency-Key": "wp12-agent-run"},
        json={**request, "expected_plan_digest": preview.json()["plan_digest"]},
    )
    assert submitted.status_code == 202
    pending = submitted.json()
    assert pending["status"] == "pending"
    with database.cursor() as cursor:
        assert cursor.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0

    _session_for(client, main_module, REVIEWER_ID)
    decision = client.post(
        f"/api/v2/approvals/{pending['approval_id']}/decisions",
        headers={"Idempotency-Key": "wp12-human-run"},
        json={"decision": "approve", "note": "WP12 offline acceptance"},
    )
    assert decision.status_code == 202
    approved = decision.json()
    assert approved["status"] == "approved"
    job = database.get_job(approved["job_id"])
    assert job is not None and job.status == "queued"
    assert job.pin_server == "offline-rental"

    # Steps 11-12: dispatch through the production attempt orchestrator.  The
    # fake records the exact reviewed mkdir/write/tmux contract without network.
    lease = database.acquire_scheduler_lease(
        owner_id=main_module.app_state.execution_scheduler_owner_id,
        lease_seconds=120,
    )
    ssh = ScriptedSSH()
    outcome = asyncio.run(
        dispatch_job_via_attempt(
            database,
            ssh.run,
            ssh.write_file,
            job=job,
            server_config_revision_id=rental_revision["id"],
            leader_owner_id=lease["owner_id"],
            scheduler_fencing_epoch=lease["fencing_epoch"],
        )
    )
    assert outcome.state == "running"
    attempt_target = database.get_execution_attempt(outcome.attempt_id)
    assert attempt_target["server_name"] == "offline-rental"
    assert attempt_target["server_config_revision_id"] == rental_revision["id"]
    assert '"port":22022' in database.get_active_server_config_revision(
        attempt_target["server_name"]
    )["normalized_target_json"]
    assert any("launch.sh" in command for _, command, _ in ssh.calls)
    assert any(path.endswith("launch.sh") and "tmux" in content for _, path, content in ssh.writes)
    assert {path.rsplit("/", 1)[-1] for _, path, _ in ssh.writes} >= {
        "cmd.sh", "run.sh", "launch.sh"
    }

    attempt = database.get_execution_attempt(outcome.attempt_id)
    terminal = _terminalize_attempt(main_module, attempt, exit_code=0)
    database.update_job(job.id, log_tail="epoch 12 loss=0.125\n")

    # Steps 13-15: collect platform-owned artifact metadata and parsed metrics,
    # then read the bounded Product Run surfaces used by the WP9 MCP tools.
    encoded_path = "cmVzdWx0cy9tZXRyaWNzLmpzb24="  # results/metrics.json

    async def metadata_ssh(_server, command, _timeout):
        assert "ARTIFACT_COLLECTION_COMPLETE" in command
        return type(
            "Result",
            (),
            {
                "exit_status": 0,
                "stdout": (
                    f"ARTIFACT\t{encoded_path}\tfile\t42\t{'c' * 64}\n"
                    "ARTIFACT_COLLECTION_COMPLETE\n"
                ),
            },
        )()

    lease = database.acquire_scheduler_lease(
        owner_id=main_module.app_state.execution_scheduler_owner_id,
        lease_seconds=120,
    )
    assert asyncio.run(
        collect_attempt(
            database,
            metadata_ssh,
            attempt=terminal,
            job_id=job.id,
            leader_owner_id=lease["owner_id"],
            scheduler_fencing_epoch=lease["fencing_epoch"],
        )
    ) == "collection_delivered"
    database.replace_run_metrics(
        job.id,
        (MetricsEntry(key="loss", value_type="decimal", value_text="0.125"),),
        status="collected",
        reason=None,
        source_sha256="d" * 64,
    )

    _session_for(client, main_module, OPERATOR_ID)
    plan_id = approved["execution_plan_id"]
    detail = client.get(f"/api/v2/runs/{plan_id}")
    artifacts = client.get(f"/api/v2/runs/{plan_id}/artifacts")
    metrics = client.get(f"/jobs/{job.id}/metrics")
    log = client.get(f"/jobs/{job.id}/log", params={"lines": 20})
    assert detail.status_code == artifacts.status_code == metrics.status_code == log.status_code == 200
    assert detail.json()["state"] == "succeeded"
    assert artifacts.json()["metadata_only"] is True
    assert artifacts.json()["items"][0]["relative_path"] == "results/metrics.json"
    assert metrics.json()["collection_status"] == "collected"
    assert metrics.json()["metrics"][0]["value_text"] == "0.125"
    assert log.json()["log_tail"] == "epoch 12 loss=0.125\n"

    # Step 15 exercises the actual WP9 MCP bridge, backed by this same TestClient
    # and therefore this exact persisted Run rather than a separately mocked API.
    mcp_run = _mcp_against_test_client(client, "get_run", {"plan_id": plan_id})
    mcp_metrics = _mcp_against_test_client(
        client, "get_run_metrics", {"plan_id": plan_id}
    )
    mcp_artifacts = _mcp_against_test_client(
        client, "get_run_artifacts", {"plan_id": plan_id, "limit": 25}
    )
    mcp_log = _mcp_against_test_client(
        client, "get_run_log_tail", {"plan_id": plan_id, "lines": 20}
    )
    mcp_run_identity = mcp_run.get("summary", mcp_run)
    assert mcp_run_identity.get("plan_id") == plan_id
    assert mcp_run_identity.get("state") == "succeeded"
    assert mcp_metrics["availability"] == "known"
    assert mcp_metrics["metrics"][0]["value_text"] == "0.125"
    assert mcp_artifacts["metadata_only"] is True
    assert mcp_log["log_tail"] == "epoch 12 loss=0.125\n"

    # Governance and leakage acceptance: both request and decision audit records
    # exist, and secret-bearing target details never enter Product API payloads
    # or the persisted audit stream.
    with database.cursor() as cursor:
        audit_rows = [dict(row) for row in cursor.execute("SELECT * FROM audit_events").fetchall()]
        actions = {
            row["action"] for row in audit_rows
        }
    assert "approval_created" in actions
    assert "approval_decided" in actions
    exposed = "\n".join((detail.text, artifacts.text, metrics.text, log.text))
    audit = repr(audit_rows)
    for secret in (
        "/dispatch-test/nonexistent-pr05-key", "/tmp/offline-rental-key",
        "192.0.2.117",
    ):
        assert secret not in exposed
        assert secret not in audit

    # The matrix itself is a reviewable completeness assertion, while this test
    # supplies live composed proof rather than merely checking test names.
    assert tuple(WP12_STEP_PROOF) == tuple(range(1, 19))
    assert {kind for kind, _ in WP12_STEP_PROOF.values()} == {
        "direct", "journey", "studio"
    }
    assert set(WP12_LEAK_PROOF) == {"api_and_audit", "prompt", "transcript", "ui"}


def test_wp12_unknown_transport_preserves_execution_truth(api_client):
    """An ambiguous launch remains unknown and never becomes execution failure."""
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    session = _agent_session(main_module.app_state.db, seed)
    _session_for(client, main_module, OPERATOR_ID)
    request = _preview_request(seed)
    preview = client.post(f"/api/v2/agent-sessions/{session.id}/run-previews", json=request).json()
    pending = client.post(
        f"/api/v2/agent-sessions/{session.id}/run-requests",
        headers={"Idempotency-Key": "wp12-failure-run"},
        json={**request, "expected_plan_digest": preview["plan_digest"]},
    ).json()
    _session_for(client, main_module, REVIEWER_ID)
    approved = client.post(
        f"/api/v2/approvals/{pending['approval_id']}/decisions",
        headers={"Idempotency-Key": "wp12-failure-approve"},
        json={"decision": "approve"},
    ).json()
    database = main_module.app_state.db
    lease = database.acquire_scheduler_lease(
        owner_id=main_module.app_state.execution_scheduler_owner_id, lease_seconds=120
    )
    unknown = asyncio.run(
        dispatch_job_via_attempt(
            database,
            ScriptedSSH(fail_on="launch.sh", exc=TimeoutError()).run,
            ScriptedSSH().write_file,
            job=database.get_job(approved["job_id"]),
            server_config_revision_id=seed["revision"]["id"],
            leader_owner_id=lease["owner_id"],
            scheduler_fencing_epoch=lease["fencing_epoch"],
        )
    )
    assert unknown.state == "unknown" and unknown.requeued is False
    assert database.get_job(approved["job_id"]).status == "running"
    run = client.get(f"/api/v2/runs/{approved['execution_plan_id']}").json()
    assert run["current_attempt"]["liveness"] == "unknown"
    assert run["state"] != "failed"


def test_wp12_collection_failure_does_not_rewrite_terminal_execution_truth(api_client):
    """A result transport/parser failure affects evidence, not execution truth."""
    client, main_module = api_client
    approved, attempt = _running_product_run(client, main_module)
    database = main_module.app_state.db
    terminal = _terminalize_attempt(main_module, attempt, exit_code=0)

    async def failed_collection(_server, _command, _timeout):
        raise ConnectionError("offline acceptance transport loss")

    lease = database.acquire_scheduler_lease(
        owner_id=main_module.app_state.execution_scheduler_owner_id,
        lease_seconds=120,
    )
    outcome = asyncio.run(
        collect_attempt(
            database,
            failed_collection,
            attempt=terminal,
            job_id=approved["job_id"],
            leader_owner_id=lease["owner_id"],
            scheduler_fencing_epoch=lease["fencing_epoch"],
        )
    )
    assert outcome == "collection_failed"
    assert database.get_job(approved["job_id"]).status == "done"
    persisted = database.get_execution_attempt(terminal["id"])
    assert persisted["state"] == "done" and persisted["exit_code"] == 0

    _session_for(client, main_module, OPERATOR_ID)
    detail = client.get(f"/api/v2/runs/{approved['execution_plan_id']}")
    artifacts = client.get(
        f"/api/v2/runs/{approved['execution_plan_id']}/artifacts"
    )
    assert detail.status_code == artifacts.status_code == 200
    assert detail.json()["state"] in {"succeeded", "needs_attention"}
    assert detail.json()["state"] != "failed"
    assert artifacts.json()["availability"] == "unknown"
    assert artifacts.json()["complete"] is False
