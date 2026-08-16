"""Focused invariants for existing-instance Product deployment."""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.config import ServerConfig
from app.authorization import _valid_membership_approval_payload
from app.execution_contract import canonical_json, utf8_sha256
from dispatch_center.api.routers import project_instance_update_v2 as update_router
from tests.test_execution_plan_v2_api import _enable_execution_plan_v2, _seed_execution_context
from tests.test_run_templates_v2 import OPERATOR_ID, OWNER_ID, _session_for

import pytest

from dispatch_center.api.routers.project_instance_update_v2 import (
    InstanceUpdatePreviewRequest,
    _probe,
)


def test_existing_inventory_instance_id_is_accepted_but_arbitrary_text_is_not() -> None:
    request = InstanceUpdatePreviewRequest(
        project_version_id="11111111-1111-4111-8111-111111111111",
        instance_id="3e654f6db4ef1228",
    )
    assert request.instance_id == "3e654f6db4ef1228"
    with pytest.raises(ValueError):
        InstanceUpdatePreviewRequest(
            project_version_id="11111111-1111-4111-8111-111111111111",
            instance_id="not-an-instance",
        )


def test_authorization_payload_validator_accepts_canonical_instance_uuid() -> None:
    payload = {
        "project_id": "11111111-1111-4111-8111-111111111111",
        "project_version_id": "22222222-2222-4222-8222-222222222222",
        "instance_id": "33333333-3333-4333-8333-333333333333",
        "server_config_revision_id": "44444444-4444-4444-8444-444444444444",
        "target_identity_sha256": "a" * 64,
        "server_name": "pilot-117",
        "git_commit": "b" * 40,
        "hub_ref": "refs/heads/codex-promoted/22222222-2222-4222-8222-222222222222",
        "promotion_approval_id": 1,
        "promotion_bundle_sha256": "c" * 64,
        "expected_before_commit": "d" * 40,
        "expected_before_branch": None,
        "path_sha256": "e" * 64,
        "checkout_before_digest": "f" * 64,
    }
    payload["preview_digest"] = utf8_sha256(canonical_json(payload))
    assert _valid_membership_approval_payload("project_instance_update_v2", payload)


def test_probe_preserves_blank_detached_branch_and_never_accepts_dirty() -> None:
    class Result:
        exit_status = 0
        stdout = "DC_COMMIT:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\nDC_BRANCH:\nDC_DIRTY:0\n"

    async def ssh_run(_server: str, _command: str, _timeout: int):
        return Result()

    observed = asyncio.run(_probe(ssh_run, "117", "/private/path"))
    assert observed == {
        "commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "branch": "",
        "dirty": False,
    }


def test_update_module_prohibits_destructive_checkout_recovery_commands() -> None:
    source = (
        Path(__file__).parents[1] / "dispatch_center/api/routers/project_instance_update_v2.py"
    ).read_text(encoding="utf-8")
    assert "checkout --detach" in source
    assert "git bundle verify" in source
    assert "reset --hard" not in source
    assert "git stash" not in source


class _Result:
    def __init__(self, stdout: str = "", exit_status: int = 0) -> None:
        self.stdout = stdout
        self.exit_status = exit_status


class _UpdateRuntime:
    def __init__(
        self,
        main_module,
        before: str,
        desired: str,
        *,
        lose_checkout_response: bool = False,
        checkout_reaches_target: bool = True,
        target_branch: str = "",
    ) -> None:
        self.config = main_module.app_state.config
        self.server_configs = {
            "pilot-117": ServerConfig(
                name="pilot-117", host="192.0.2.117", user="worker",
                key="/dispatch-test/nonexistent-pr05-key", project_roots=["/srv/projects"],
                dataset_roots=["/srv/datasets"], execution_backend="ssh",
            )
        }
        self.before, self.desired, self.current = before, desired, before
        self.calls: list[str] = []
        self.lose_checkout_response = lose_checkout_response
        self.checkout_reaches_target = checkout_reaches_target
        self.target_branch = target_branch

    async def ssh_run(self, _server: str, command: str, _timeout: int):
        self.calls.append(command)
        if "checkout --detach" in command:
            if self.checkout_reaches_target:
                self.current = self.desired
            if self.lose_checkout_response:
                self.lose_checkout_response = False
                raise RuntimeError("simulated response loss")
            return _Result()
        branch = "main" if self.current == self.before else self.target_branch
        return _Result(f"DC_COMMIT:{self.current}\nDC_BRANCH:{branch}\nDC_DIRTY:0\n")


def test_instance_update_preview_request_replays_without_reprobing_after_state_drift(
    api_client, monkeypatch
) -> None:
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    desired, before = "a" * 40, "c" * 40
    main_module.app_state.db.update_instance_reconcile(
        seed["instance_id"], state="available", git_branch="main", git_commit=before,
        dirty=False, touch_last_seen=True,
    )
    runtime = _UpdateRuntime(main_module, before, desired)
    main_module.app.state.dispatch_runtime = runtime
    local_calls: list[str] = []

    async def local(command: str, _timeout: int):
        local_calls.append(command)
        if "rev-parse --verify" in command:
            return _Result(f"{desired}\n")
        return _Result()

    monkeypatch.setattr(update_router, "local_run", local)
    _session_for(client, main_module, OPERATOR_ID)
    body = {"project_version_id": seed["version"]["id"], "instance_id": seed["instance_id"]}
    preview = client.post(
        f"/api/v2/projects/{seed['project_id']}/instance-update-previews", json=body
    )
    assert preview.status_code == 200, preview.json()
    payload = preview.json()["payload"]
    assert "/srv/projects/product-v2" not in canonical_json(payload)
    request = client.post(
        f"/api/v2/projects/{seed['project_id']}/instance-update-requests",
        json={**body, "expected_preview_digest": preview.json()["preview_digest"]},
        headers={"Idempotency-Key": "instance-update-request-1"},
    )
    assert request.status_code == 202, request.json()
    approval_id = request.json()["approval_id"]
    calls_before_replay = len(runtime.calls)
    main_module.app_state.db.update_instance_reconcile(
        seed["instance_id"], state="unknown"
    )
    replay = client.post(
        f"/api/v2/projects/{seed['project_id']}/instance-update-requests",
        json={**body, "expected_preview_digest": preview.json()["preview_digest"]},
        headers={"Idempotency-Key": "instance-update-request-1"},
    )
    assert replay.status_code == 202, replay.json()
    assert replay.json()["approval_id"] == approval_id
    assert replay.json()["replayed"] is True
    assert len(runtime.calls) == calls_before_replay


def test_instance_update_approval_detaches_exact_commit_and_terminal_retry_never_ssh(
    api_client, monkeypatch
) -> None:
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    desired, before = "a" * 40, "c" * 40
    main_module.app_state.db.update_instance_reconcile(
        seed["instance_id"], state="available", git_branch="main", git_commit=before,
        dirty=False, touch_last_seen=True,
    )
    runtime = _UpdateRuntime(main_module, before, desired)
    main_module.app.state.dispatch_runtime = runtime
    local_calls: list[str] = []

    async def local(command: str, _timeout: int):
        local_calls.append(command)
        if command.endswith("rev-parse --verify HEAD"):
            return _Result("d" * 40 + "\n")
        if "rev-parse --verify" in command:
            return _Result(desired + "\n")
        return _Result()

    monkeypatch.setattr(update_router, "local_run", local)
    _session_for(client, main_module, OPERATOR_ID)
    body = {"project_version_id": seed["version"]["id"], "instance_id": seed["instance_id"]}
    preview = client.post(f"/api/v2/projects/{seed['project_id']}/instance-update-previews", json=body)
    assert preview.status_code == 200, preview.json()
    created = client.post(
        f"/api/v2/projects/{seed['project_id']}/instance-update-requests",
        json={**body, "expected_preview_digest": preview.json()["preview_digest"]},
        headers={"Idempotency-Key": "instance-update-request-2"},
    )
    assert created.status_code == 202, created.json()
    approval_id = created.json()["approval_id"]
    assert main_module.app_state.db.get_verified_product_approval(approval_id) is not None
    _session_for(client, main_module, OWNER_ID)
    assert client.get(f"/api/v2/approvals/{approval_id}").status_code == 200
    decided = client.post(
        f"/api/v2/approvals/{approval_id}/decisions", json={"decision": "approve", "note": "reviewed"},
        headers={"Idempotency-Key": "instance-update-decision-2"},
    )
    assert decided.status_code == 202, decided.json()
    assert decided.json()["status"] == "approved"
    calls = "\n".join(runtime.calls)
    assert "git bundle verify" in "\n".join(local_calls)
    assert "fetch --no-tags" in calls and "checkout --detach" in calls
    assert "reset --hard" not in calls and "stash" not in calls
    instance = next(item for item in main_module.app_state.db.list_all_project_instances() if item.id == seed["instance_id"])
    assert (instance.git_commit, instance.git_branch, instance.state, instance.dirty) == (desired, None, "diverged", False)
    call_count = len(runtime.calls)
    replay = client.post(
        f"/api/v2/approvals/{approval_id}/decisions", json={"decision": "approve", "note": "reviewed"},
        headers={"Idempotency-Key": "instance-update-decision-2"},
    )
    assert replay.status_code == 202, replay.json()
    assert replay.json()["replayed"] is True
    assert len(runtime.calls) == call_count


def test_instance_update_response_loss_reconciles_exact_checkout_without_replay(
    api_client, monkeypatch
) -> None:
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    desired, before = "a" * 40, "c" * 40
    main_module.app_state.db.update_instance_reconcile(
        seed["instance_id"], state="available", git_branch="main", git_commit=before,
        dirty=False, touch_last_seen=True,
    )
    runtime = _UpdateRuntime(
        main_module, before, desired, lose_checkout_response=True,
    )
    main_module.app.state.dispatch_runtime = runtime

    async def local(command: str, _timeout: int):
        if command.endswith("rev-parse --verify HEAD"):
            return _Result("d" * 40 + "\n")
        if "rev-parse --verify" in command:
            return _Result(desired + "\n")
        return _Result()

    monkeypatch.setattr(update_router, "local_run", local)
    _session_for(client, main_module, OPERATOR_ID)
    body = {"project_version_id": seed["version"]["id"], "instance_id": seed["instance_id"]}
    preview = client.post(f"/api/v2/projects/{seed['project_id']}/instance-update-previews", json=body)
    created = client.post(
        f"/api/v2/projects/{seed['project_id']}/instance-update-requests",
        json={**body, "expected_preview_digest": preview.json()["preview_digest"]},
        headers={"Idempotency-Key": "instance-update-response-loss-request"},
    )
    assert created.status_code == 202, created.json()
    approval_id = created.json()["approval_id"]
    _session_for(client, main_module, OWNER_ID)
    lost = client.post(
        f"/api/v2/approvals/{approval_id}/decisions", json={"decision": "approve"},
        headers={"Idempotency-Key": "instance-update-response-loss-first"},
    )
    assert lost.status_code == 409, lost.json()
    assert main_module.app_state.db.get_approval(approval_id).status == "pending"
    calls_before_retry = len(runtime.calls)
    reconciled = client.post(
        f"/api/v2/approvals/{approval_id}/decisions", json={"decision": "approve"},
        headers={"Idempotency-Key": "instance-update-response-loss-retry"},
    )
    assert reconciled.status_code == 202, reconciled.json()
    assert reconciled.json()["status"] == "approved"
    retry_calls = runtime.calls[calls_before_retry:]
    assert len(retry_calls) == 1
    assert "checkout --detach" not in retry_calls[0]
    assert "fetch --no-tags" not in retry_calls[0]


def test_instance_update_response_loss_with_nonmatching_checkout_stays_on_hold(
    api_client, monkeypatch
) -> None:
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    desired, before = "a" * 40, "c" * 40
    main_module.app_state.db.update_instance_reconcile(
        seed["instance_id"], state="available", git_branch="main", git_commit=before,
        dirty=False, touch_last_seen=True,
    )
    runtime = _UpdateRuntime(
        main_module, before, desired, lose_checkout_response=True,
        checkout_reaches_target=False,
    )
    main_module.app.state.dispatch_runtime = runtime

    async def local(command: str, _timeout: int):
        if "rev-parse --verify" in command:
            return _Result(desired + "\n")
        return _Result()

    monkeypatch.setattr(update_router, "local_run", local)
    _session_for(client, main_module, OPERATOR_ID)
    body = {"project_version_id": seed["version"]["id"], "instance_id": seed["instance_id"]}
    preview = client.post(f"/api/v2/projects/{seed['project_id']}/instance-update-previews", json=body)
    created = client.post(
        f"/api/v2/projects/{seed['project_id']}/instance-update-requests",
        json={**body, "expected_preview_digest": preview.json()["preview_digest"]},
        headers={"Idempotency-Key": "instance-update-hold-request"},
    )
    approval_id = created.json()["approval_id"]
    _session_for(client, main_module, OWNER_ID)
    assert client.post(
        f"/api/v2/approvals/{approval_id}/decisions", json={"decision": "approve"},
        headers={"Idempotency-Key": "instance-update-hold-first"},
    ).status_code == 409
    calls_before_retry = len(runtime.calls)
    held = client.post(
        f"/api/v2/approvals/{approval_id}/decisions", json={"decision": "approve"},
        headers={"Idempotency-Key": "instance-update-hold-retry"},
    )
    assert held.status_code == 409, held.json()
    assert main_module.app_state.db.get_approval(approval_id).status == "pending"
    retry_calls = runtime.calls[calls_before_retry:]
    assert len(retry_calls) == 1
    assert "checkout --detach" not in retry_calls[0]
    assert "fetch --no-tags" not in retry_calls[0]


def test_instance_update_cannot_be_rejected_after_intent_is_claimed(
    api_client, monkeypatch
) -> None:
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    desired, before = "a" * 40, "c" * 40
    main_module.app_state.db.update_instance_reconcile(
        seed["instance_id"], state="available", git_branch="main", git_commit=before,
        dirty=False, touch_last_seen=True,
    )
    runtime = _UpdateRuntime(main_module, before, desired)
    main_module.app.state.dispatch_runtime = runtime

    async def local(command: str, _timeout: int):
        if "rev-parse --verify" in command:
            return _Result(desired + "\n")
        return _Result()

    monkeypatch.setattr(update_router, "local_run", local)
    _session_for(client, main_module, OPERATOR_ID)
    body = {"project_version_id": seed["version"]["id"], "instance_id": seed["instance_id"]}
    preview = client.post(f"/api/v2/projects/{seed['project_id']}/instance-update-previews", json=body)
    created = client.post(
        f"/api/v2/projects/{seed['project_id']}/instance-update-requests",
        json={**body, "expected_preview_digest": preview.json()["preview_digest"]},
        headers={"Idempotency-Key": "instance-update-reject-after-intent-request"},
    )
    approval_id = created.json()["approval_id"]
    approval = main_module.app_state.db.get_verified_product_approval(approval_id)
    assert approval is not None
    main_module.app_state.db.begin_project_instance_update_intent(
        approval_id=approval_id, payload=approval.payload, decision_actor_id=OWNER_ID,
    )
    _session_for(client, main_module, OWNER_ID)
    rejected = client.post(
        f"/api/v2/approvals/{approval_id}/decisions", json={"decision": "reject"},
        headers={"Idempotency-Key": "instance-update-reject-after-intent-decision"},
    )
    assert rejected.status_code == 409, rejected.json()
    assert main_module.app_state.db.get_approval(approval_id).status == "pending"


def test_response_loss_recovery_on_named_branch_remains_on_hold(
    api_client, monkeypatch
) -> None:
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    desired, before = "a" * 40, "c" * 40
    main_module.app_state.db.update_instance_reconcile(
        seed["instance_id"], state="available", git_branch="main", git_commit=before,
        dirty=False, touch_last_seen=True,
    )
    runtime = _UpdateRuntime(
        main_module, before, desired, lose_checkout_response=True,
        target_branch="release",
    )
    main_module.app.state.dispatch_runtime = runtime

    async def local(command: str, _timeout: int):
        if "rev-parse --verify" in command:
            return _Result(desired + "\n")
        return _Result()

    monkeypatch.setattr(update_router, "local_run", local)
    _session_for(client, main_module, OPERATOR_ID)
    body = {"project_version_id": seed["version"]["id"], "instance_id": seed["instance_id"]}
    preview = client.post(f"/api/v2/projects/{seed['project_id']}/instance-update-previews", json=body)
    created = client.post(
        f"/api/v2/projects/{seed['project_id']}/instance-update-requests",
        json={**body, "expected_preview_digest": preview.json()["preview_digest"]},
        headers={"Idempotency-Key": "instance-update-named-recovery-request"},
    )
    approval_id = created.json()["approval_id"]
    _session_for(client, main_module, OWNER_ID)
    assert client.post(
        f"/api/v2/approvals/{approval_id}/decisions", json={"decision": "approve"},
        headers={"Idempotency-Key": "instance-update-named-recovery-first"},
    ).status_code == 409
    calls_before_retry = len(runtime.calls)
    held = client.post(
        f"/api/v2/approvals/{approval_id}/decisions", json={"decision": "approve"},
        headers={"Idempotency-Key": "instance-update-named-recovery-retry"},
    )
    assert held.status_code == 409, held.json()
    assert main_module.app_state.db.get_approval(approval_id).status == "pending"
    retry_calls = runtime.calls[calls_before_retry:]
    assert len(retry_calls) == 1
    assert "checkout --detach" not in retry_calls[0]


def test_post_checkout_named_branch_remains_on_recovery_hold(api_client, monkeypatch) -> None:
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    desired, before = "a" * 40, "c" * 40
    main_module.app_state.db.update_instance_reconcile(
        seed["instance_id"], state="available", git_branch="main", git_commit=before,
        dirty=False, touch_last_seen=True,
    )
    runtime = _UpdateRuntime(main_module, before, desired, target_branch="release")
    main_module.app.state.dispatch_runtime = runtime

    async def local(command: str, _timeout: int):
        if "rev-parse --verify" in command:
            return _Result(desired + "\n")
        return _Result()

    monkeypatch.setattr(update_router, "local_run", local)
    _session_for(client, main_module, OPERATOR_ID)
    body = {"project_version_id": seed["version"]["id"], "instance_id": seed["instance_id"]}
    preview = client.post(f"/api/v2/projects/{seed['project_id']}/instance-update-previews", json=body)
    created = client.post(
        f"/api/v2/projects/{seed['project_id']}/instance-update-requests",
        json={**body, "expected_preview_digest": preview.json()["preview_digest"]},
        headers={"Idempotency-Key": "instance-update-named-post-effect-request"},
    )
    approval_id = created.json()["approval_id"]
    _session_for(client, main_module, OWNER_ID)
    held = client.post(
        f"/api/v2/approvals/{approval_id}/decisions", json={"decision": "approve"},
        headers={"Idempotency-Key": "instance-update-named-post-effect-decision"},
    )
    assert held.status_code == 409, held.json()
    assert main_module.app_state.db.get_approval(approval_id).status == "pending"
    assert any("checkout --detach" in command for command in runtime.calls)


def test_response_loss_recovery_with_path_drift_never_probes(api_client, monkeypatch) -> None:
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    desired, before = "a" * 40, "c" * 40
    main_module.app_state.db.update_instance_reconcile(
        seed["instance_id"], state="available", git_branch="main", git_commit=before,
        dirty=False, touch_last_seen=True,
    )
    runtime = _UpdateRuntime(
        main_module, before, desired, lose_checkout_response=True,
    )
    main_module.app.state.dispatch_runtime = runtime

    async def local(command: str, _timeout: int):
        if "rev-parse --verify" in command:
            return _Result(desired + "\n")
        return _Result()

    monkeypatch.setattr(update_router, "local_run", local)
    _session_for(client, main_module, OPERATOR_ID)
    body = {"project_version_id": seed["version"]["id"], "instance_id": seed["instance_id"]}
    preview = client.post(f"/api/v2/projects/{seed['project_id']}/instance-update-previews", json=body)
    created = client.post(
        f"/api/v2/projects/{seed['project_id']}/instance-update-requests",
        json={**body, "expected_preview_digest": preview.json()["preview_digest"]},
        headers={"Idempotency-Key": "instance-update-path-drift-request"},
    )
    approval_id = created.json()["approval_id"]
    _session_for(client, main_module, OWNER_ID)
    assert client.post(
        f"/api/v2/approvals/{approval_id}/decisions", json={"decision": "approve"},
        headers={"Idempotency-Key": "instance-update-path-drift-first"},
    ).status_code == 409
    with main_module.app_state.db._immediate_cursor() as cursor:
        cursor.execute(
            "UPDATE project_instances SET path = ? WHERE id = ?",
            ("/srv/projects/drifted", seed["instance_id"]),
        )
    calls_before_retry = len(runtime.calls)
    held = client.post(
        f"/api/v2/approvals/{approval_id}/decisions", json={"decision": "approve"},
        headers={"Idempotency-Key": "instance-update-path-drift-retry"},
    )
    assert held.status_code == 409, held.json()
    assert main_module.app_state.db.get_approval(approval_id).status == "pending"
    assert len(runtime.calls) == calls_before_retry
