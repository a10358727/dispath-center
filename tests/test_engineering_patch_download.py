"""Authenticated, fail-closed sanitized Engineering Task patch downloads."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest

import app.engineering_presentation as engineering_presentation
import app.engineering_tasks as engineering_tasks
from app.audit import read_audit
from app.engineering_path_policy import build_engineering_path_policy
from app.results import local_result_dir


def _error_message(response):
    """Legacy routes answered `{"detail": ...}`; the v2 routes (the only
    surface since DG-CONSOLIDATION-v1 C-5 (c)) answer the APIError envelope."""
    body = response.json()
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return body["error"].get("message")
    return body.get("detail") if isinstance(body, dict) else None


BASE_COMMIT = "a" * 40
RESULT_COMMIT = "b" * 40
TOKEN = "synthetic-patch-download-token-1234567890"


def _create_downloadable_task(
    main_module,
    tmp_path: Path,
    *,
    contract_version: str = "engineering-task-v2",
    patch_bytes: bytes = b"diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n+safe\n",
) -> dict:
    db = main_module.app_state.db
    main_module.app_state.config.local_home_dir = str(tmp_path)
    project = f"patch-project-{uuid.uuid4().hex[:8]}"
    project_id = db.insert_project(project, f"https://example.invalid/{project}.git")
    version = db.get_or_create_project_version(project, BASE_COMMIT, git_ref="main")
    task_id = str(uuid.uuid4())
    source = f"engineering_bundles/{task_id}.bundle"
    execution_contract = {
        "runner": {
            "name": "server-a",
            "host": "192.0.2.10",
            "user": "runner",
            "port": 22,
        },
        "workspace_rel": "codex_workspaces",
        "source_kind": "hub_bundle",
        "source": source,
        "network_access": False,
        "dependency_installation": False,
    }
    if contract_version == "engineering-task-v2":
        path_policy, path_policy_sha256 = build_engineering_path_policy(
            ["app/"], []
        )
        execution_contract.update(
            {
                "path_policy": path_policy,
                "path_policy_sha256": path_policy_sha256,
                "path_verifier": path_policy["verifier"],
            }
        )
    provider_capabilities = {
        "provider_id": "codex",
        "adapter": "codex-exec-v1",
    }
    structured_request = {
        "objective": "Produce a collected patch",
        "allowed_paths": ["app/"],
        "prohibited_paths": [],
    }
    instruction = "AI Engineering Task\n\nTask objective:\n- Produce a patch"
    detected_metadata = {"detection": "path-markers-v1", "languages": ["python"]}
    approval_payload = {
        "contract_version": contract_version,
        "engineering_task_id": task_id,
        "project": project,
        "project_id": project_id,
        "project_version_id": version.id,
        "base_commit": BASE_COMMIT,
        "agent_provider_id": "codex",
        "provider_capabilities": provider_capabilities,
        "execution_contract": execution_contract,
        "structured_request": structured_request,
        "instruction": instruction,
        "detected_metadata": detected_metadata,
        "validation_target": None,
        "runner_server": "server-a",
        "source_kind": "hub_bundle",
        "source": source,
        "network_access": False,
        "dependency_installation": False,
    }
    inserted_task_id, approval_id = db.insert_engineering_task_request(
        task_id=task_id,
        project_id=project_id,
        project_name=project,
        project_version_id=version.id,
        base_commit=BASE_COMMIT,
        agent_provider_id="codex",
        provider_capabilities=provider_capabilities,
        execution_contract=execution_contract,
        contract_version=contract_version,
        structured_request=structured_request,
        instruction=instruction,
        detected_metadata=detected_metadata,
        runner_server="server-a",
        validation_target=None,
        approval_payload=approval_payload,
    )
    assert inserted_task_id == task_id
    run_id, staging_job_id, coding_job_id = db.finalize_engineering_task_approval_plan(
        task_id=task_id,
        approval_id=approval_id,
        project=project,
        runner_server="server-a",
        instruction=instruction,
        base_commit=BASE_COMMIT,
        project_version_id=version.id,
        validation_target=None,
        worktree_path=f"codex_workspaces/tasks/{approval_id}/repo",
        staging_command="stage immutable input",
        coding_command="run reviewed codex adapter",
        approval_note="approved for patch download test",
        decision_actor_id=None,
        decision_mechanism="manual",
    )
    db.update_job(
        staging_job_id,
        status="done",
        server="_local",
        exit_code=0,
        started_at="2026-07-15T01:00:00+00:00",
        finished_at="2026-07-15T01:00:01+00:00",
    )
    db.update_job(
        coding_job_id,
        status="done",
        server="server-a",
        exit_code=0,
        started_at="2026-07-15T01:00:02+00:00",
        finished_at="2026-07-15T01:00:03+00:00",
    )
    result_dir = Path(local_result_dir(coding_job_id, str(tmp_path)))
    result_dir.mkdir(parents=True)
    diff_path = result_dir / "diff.patch"
    bundle_path = result_dir / "changes.bundle"
    diff_path.write_bytes(patch_bytes)
    bundle_bytes = b"collected bundle descriptor fixture"
    bundle_path.write_bytes(bundle_bytes)
    db.update_coding_run(
        run_id,
        status="done",
        result_branch=f"ai-task-{approval_id}",
        result_commit=RESULT_COMMIT,
        bundle_path=str(bundle_path),
        finished_at="2026-07-15T01:00:03+00:00",
    )
    bundle_artifact = db.register_engineering_task_artifact(
        task_id=task_id,
        attempt_number=1,
        artifact_key="bundle",
        kind="bundle",
        label="Verified change bundle",
        storage_kind="local_result",
        storage_key="changes.bundle",
        content_type="application/x-git-bundle",
        coding_run_id=run_id,
        source_job_id=coding_job_id,
        verification_status="verified",
        redaction_status="not_applicable",
        availability="available",
    )
    db.record_engineering_task_artifact_collection(
        bundle_artifact.id,
        source_sha256=hashlib.sha256(bundle_bytes).hexdigest(),
        source_size_bytes=len(bundle_bytes),
        verification_status="verified",
        redaction_status="not_applicable",
        availability="available",
    )
    diff_artifact = db.register_engineering_task_artifact(
        task_id=task_id,
        attempt_number=1,
        artifact_key="diff",
        kind="diff",
        label="Code diff",
        storage_kind="local_result",
        storage_key="diff.patch",
        content_type="text/x-diff",
        coding_run_id=run_id,
        source_job_id=coding_job_id,
        verification_status="not_required",
        redaction_status="redacted",
        availability="available",
    )
    db.record_engineering_task_artifact_collection(
        diff_artifact.id,
        source_sha256=hashlib.sha256(patch_bytes).hexdigest(),
        source_size_bytes=len(patch_bytes),
        verification_status="not_required",
        redaction_status="redacted",
        availability="available",
    )
    return {
        "task_id": task_id,
        "approval_id": approval_id,
        "version_id": version.id,
        "run_id": run_id,
        "coding_job_id": coding_job_id,
        "diff_artifact_id": diff_artifact.id,
        "bundle_artifact_id": bundle_artifact.id,
        "diff_path": diff_path,
        "bundle_path": bundle_path,
    }


@pytest.mark.parametrize(
    "contract_version",
    ["engineering-task-v1", "engineering-task-v2"],
)
def test_native_v1_and_v2_download_only_sanitized_collected_patch(
    api_client, tmp_path, contract_version
):
    client, main_module = api_client
    raw = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        f"+Authorization: Bearer {TOKEN}\n"
        "+/home/private/worktree/file.py\n"
    ).encode()
    fixture = _create_downloadable_task(
        main_module,
        tmp_path,
        contract_version=contract_version,
        patch_bytes=raw,
    )

    response = client.get(f"/api/v2/engineering-tasks/{fixture['task_id']}/patch")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/x-diff; charset=utf-8"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="engineering-task-{fixture["task_id"]}.redacted.patch"'
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.status_code != 200 or response.headers["x-content-type-options"] == "nosniff"  # v2 error envelope carries no nosniff
    assert response.headers["x-artifact-semantics"] == "sanitized-collected-patch"
    assert response.headers["x-engineering-patch-redacted"] == "true"
    assert int(response.headers["content-length"]) == len(response.content)
    assert len(response.content) <= 1024 * 1024
    assert TOKEN not in response.text
    assert "/home/private" not in response.text
    assert "[REDACTED]" in response.text

    action = client.get(f"/api/v2/engineering-tasks/{fixture['task_id']}").json()[
        "available_actions"
    ]["download_patch"]
    assert action == {
        "enabled": True,
        "reason": None,
        "url": f"/api/v2/engineering-tasks/{fixture['task_id']}/patch",
        "artifact_kind": "sanitized_collected_patch",
    }


def test_clean_collected_patch_uses_non_redacted_filename_and_header(api_client, tmp_path):
    client, main_module = api_client
    fixture = _create_downloadable_task(main_module, tmp_path)

    response = client.get(f"/api/v2/engineering-tasks/{fixture['task_id']}/patch")

    assert response.status_code == 200
    assert response.headers["content-disposition"] == (
        f'attachment; filename="engineering-task-{fixture["task_id"]}.patch"'
    )
    assert response.headers["x-engineering-patch-redacted"] == "false"
    assert response.content == fixture["diff_path"].read_bytes()


def test_control_or_terminal_normalization_is_reported_as_redaction(
    api_client, tmp_path
):
    client, main_module = api_client
    raw = (
        b"diff --git a/app.py b/app.py\n"
        b"--- a/app.py\n+++ b/app.py\n"
        b"\x1b[31m+safe\x1b[0m\x00\n"
    )
    fixture = _create_downloadable_task(
        main_module,
        tmp_path,
        patch_bytes=raw,
    )

    response = client.get(f"/api/v2/engineering-tasks/{fixture['task_id']}/patch")

    assert response.status_code == 200
    assert response.headers["x-engineering-patch-redacted"] == "true"
    assert response.headers["content-disposition"].endswith('.redacted.patch"')
    assert b"\x1b" not in response.content
    assert b"\x00" not in response.content


def test_common_high_confidence_credential_families_are_sanitized(
    api_client, tmp_path
):
    client, main_module = api_client
    secrets = (
        "xoxb-123456789012-abcdefghijklmnopqrstuvwxyz",
        "glpat-1234567890abcdefghij",
        "HF_TOKEN=hf_abcdefghijklmnopqrstuvwxyz1234567890",
        "npm_abcdefghijklmnopqrstuvwxyz123456",
        "pypi-AgEIabcdefghijklmnopqrstuvwxyz123456",
        "AIzaabcdefghijklmnopqrstuvwxyz1234567890",
        "sk_live_abcdefghijklmnopqrstuvwxyz",
        "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI_K7MDENG_bPxRfiCYEXAMPLEKEY",
        "postgres://dispatch-user:database-password@db.invalid/dispatch",
    )
    raw = (
        "diff --git a/config.example b/config.example\n"
        "--- a/config.example\n"
        "+++ b/config.example\n"
        + "\n".join(f"+{secret}" for secret in secrets)
        + "\n"
    ).encode()
    fixture = _create_downloadable_task(
        main_module,
        tmp_path,
        patch_bytes=raw,
    )

    response = client.get(f"/api/v2/engineering-tasks/{fixture['task_id']}/patch")

    assert response.status_code == 200
    assert response.headers["x-engineering-patch-redacted"] == "true"
    assert response.headers["content-disposition"].endswith('.redacted.patch"')
    assert response.text.count("[REDACTED]") == len(secrets)
    assert all(secret not in response.text for secret in secrets)


def test_basic_authorization_and_password_ssh_userinfo_are_sanitized_after_normalization(
    tmp_path,
):
    basic_token = "ZGlzcGF0Y2g6c2VjcmV0"
    ssh_password = "runner-password"
    safe_ssh_url = "ssh://git@code.invalid/project.git"
    raw = (
        "diff --git a/config.example b/config.example\n"
        "--- a/config.example\n"
        "+++ b/config.example\n"
        f"+Authorization: Ba\x1b[31msic {basic_token}\n"
        f"+ssh://dispatch:runner\u200b-password@worker.invalid/project.git\n"
        f"+{safe_ssh_url}\n"
    )
    (tmp_path / "diff.patch").write_text(raw, encoding="utf-8")

    captured = engineering_tasks.capture_sanitized_engineering_patch(
        result_dir=str(tmp_path)
    )

    assert captured["available"] is True
    assert captured["redacted"] is True
    sanitized = captured["_sanitized_payload"].decode("utf-8")
    assert sanitized.count("[REDACTED]") == 2
    assert basic_token not in sanitized
    assert ssh_password not in sanitized
    assert "\x1b" not in sanitized
    assert "\u200b" not in sanitized
    assert safe_ssh_url in sanitized


@pytest.mark.parametrize(
    ("patch_bytes", "expected_detail"),
    [
        (
            b"diff --git a/a b/a\n-----BEGIN PRIVATE KEY-----\nsynthetic\n",
            "AI Engineering Task patch 因安全政策而隱藏",
        ),
        (
            b"diff --git a/a b/a\n-----BEGIN \x1b[31mPRIVATE KEY-----\nsynthetic\n",
            "AI Engineering Task patch 因安全政策而隱藏",
        ),
        (
            b"diff --git a/a b/a\n-----BEGIN PRI\x00VATE KEY-----\nsynthetic\n",
            "AI Engineering Task patch 因安全政策而隱藏",
        ),
        (
            "diff --git a/a b/a\n-----BEGIN PRI\u200bVATE KEY-----\nsynthetic\n".encode(),
            "AI Engineering Task patch 因安全政策而隱藏",
        ),
        (
            b"diff --git a/a b/a\n-----BEGIN\tPRIVATE\tKEY-----\nsynthetic\n",
            "AI Engineering Task patch 因安全政策而隱藏",
        ),
        (b"diff --git a/a b/a\n+\xff\n", "AI Engineering Task patch 因安全政策而隱藏"),
        (b"", "AI Engineering Task patch 因安全政策而隱藏"),
    ],
)
def test_private_key_invalid_utf8_and_empty_payload_fail_closed(
    api_client, tmp_path, patch_bytes, expected_detail
):
    client, main_module = api_client
    fixture = _create_downloadable_task(
        main_module, tmp_path, patch_bytes=patch_bytes
    )

    response = client.get(f"/api/v2/engineering-tasks/{fixture['task_id']}/patch")

    assert response.status_code == 409
    assert _error_message(response) == expected_detail
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.status_code != 200 or response.headers["x-content-type-options"] == "nosniff"  # v2 error envelope carries no nosniff
    assert "synthetic" not in response.text


def test_oversized_source_is_rejected_before_descriptor_read(
    api_client, tmp_path, monkeypatch
):
    client, main_module = api_client
    oversized = b"x" * (engineering_tasks.ENGINEERING_TASK_SOURCE_FILE_LIMIT + 1)
    fixture = _create_downloadable_task(
        main_module, tmp_path, patch_bytes=oversized
    )

    def unexpected_capture(*, result_dir: str) -> dict:
        raise AssertionError(f"oversized patch must not be captured: {result_dir}")

    # patch the real call site (`app.engineering_presentation` imports the
    # function by name); a patch on `app.main` never reached it.
    monkeypatch.setattr(
        engineering_presentation,
        "capture_sanitized_engineering_patch",
        unexpected_capture,
    )
    response = client.get(f"/api/v2/engineering-tasks/{fixture['task_id']}/patch")

    assert response.status_code == 413
    assert _error_message(response) == "AI Engineering Task patch 超過 1 MiB 下載上限"


@pytest.mark.parametrize("drift", ["digest", "size", "symlink"])
def test_source_descriptor_drift_and_unsafe_type_return_fixed_integrity_error(
    api_client, tmp_path, drift
):
    client, main_module = api_client
    fixture = _create_downloadable_task(main_module, tmp_path)
    db = main_module.app_state.db
    if drift == "digest":
        fixture["diff_path"].write_bytes(b"x" * fixture["diff_path"].stat().st_size)
    elif drift == "size":
        fixture["diff_path"].write_bytes(fixture["diff_path"].read_bytes() + b"x")
    else:
        outside = tmp_path / "outside.patch"
        outside.write_text("diff --git a/x b/x\n", encoding="utf-8")
        fixture["diff_path"].unlink()
        fixture["diff_path"].symlink_to(outside)

    response = client.get(f"/api/v2/engineering-tasks/{fixture['task_id']}/patch")

    assert response.status_code == 409
    assert _error_message(response) == "AI Engineering Task patch 完整性驗證失敗"
    assert str(tmp_path) not in response.text
    assert db.get_engineering_task(fixture["task_id"]).status == "done"


@pytest.mark.parametrize("withheld_field", ["redaction_status", "availability"])
def test_canonical_withheld_artifact_returns_fixed_policy_error_without_reading(
    api_client, tmp_path, monkeypatch, withheld_field
):
    client, main_module = api_client
    fixture = _create_downloadable_task(main_module, tmp_path)
    db = main_module.app_state.db
    with db.cursor() as cur:
        cur.execute(
            f"UPDATE engineering_task_artifacts SET {withheld_field} = 'withheld' "
            "WHERE id = ?",
            (fixture["diff_artifact_id"],),
        )

    def unexpected_capture(*, result_dir: str) -> dict:
        raise AssertionError(f"withheld patch must not be captured: {result_dir}")

    # patch the real call site (`app.engineering_presentation` imports the
    # function by name); a patch on `app.main` never reached it.
    monkeypatch.setattr(
        engineering_presentation,
        "capture_sanitized_engineering_patch",
        unexpected_capture,
    )
    response = client.get(f"/api/v2/engineering-tasks/{fixture['task_id']}/patch")

    assert response.status_code == 409
    assert _error_message(response) == "AI Engineering Task patch 因安全政策而隱藏"


@pytest.mark.parametrize(
    "drift",
    [
        "approval_payload",
        "project_version",
        "run_owner",
        "owner_job",
        "command_digest",
        "bundle_metadata",
        "diff_metadata",
        "coordinated_source_contract",
        "coordinated_runner_contract",
        "coordinated_v2_policy_contract",
    ],
)
def test_every_persisted_contract_layer_is_revalidated(api_client, tmp_path, drift):
    client, main_module = api_client
    fixture = _create_downloadable_task(main_module, tmp_path)
    db = main_module.app_state.db
    approval = (
        db.get_approval(fixture["approval_id"])
        if drift == "approval_payload"
        else None
    )
    coordinated_task = (
        db.get_engineering_task(fixture["task_id"])
        if drift.startswith("coordinated_")
        else None
    )
    coordinated_approval = (
        db.get_approval(fixture["approval_id"])
        if drift.startswith("coordinated_")
        else None
    )
    with db.cursor() as cur:
        if drift == "approval_payload":
            assert approval is not None
            payload = {**approval.payload, "runner_server": "drifted-runner"}
            cur.execute(
                "UPDATE approvals SET payload = ? WHERE id = ?",
                (json.dumps(payload), fixture["approval_id"]),
            )
        elif drift == "project_version":
            cur.execute(
                "UPDATE project_versions SET git_commit = ? WHERE id = ?",
                ("e" * 40, fixture["version_id"]),
            )
        elif drift == "run_owner":
            cur.execute(
                "UPDATE coding_runs SET project_version_id = ? WHERE id = ?",
                (str(uuid.uuid4()), fixture["run_id"]),
            )
        elif drift == "owner_job":
            cur.execute(
                "UPDATE jobs SET server = ? WHERE id = ?",
                ("other-runner", fixture["coding_job_id"]),
            )
        elif drift == "command_digest":
            cur.execute(
                "UPDATE engineering_task_commands SET command_digest = ? WHERE job_id = ?",
                ("f" * 64, fixture["coding_job_id"]),
            )
        elif drift == "bundle_metadata":
            cur.execute(
                "UPDATE engineering_task_artifacts SET verification_status = 'rejected' "
                "WHERE id = ?",
                (fixture["bundle_artifact_id"],),
            )
        elif drift == "diff_metadata":
            cur.execute(
                "UPDATE engineering_task_artifacts SET storage_key = 'other.patch' "
                "WHERE id = ?",
                (fixture["diff_artifact_id"],),
            )
        else:
            assert coordinated_task is not None and coordinated_approval is not None
            execution_contract = dict(coordinated_task.execution_contract)
            payload = dict(coordinated_approval.payload)
            if drift == "coordinated_source_contract":
                execution_contract["source"] = "engineering_bundles/arbitrary.bundle"
                payload["source"] = execution_contract["source"]
            elif drift == "coordinated_runner_contract":
                execution_contract["runner"] = {
                    **execution_contract["runner"],
                    "name": "other-runner",
                }
            else:
                execution_contract.pop("path_policy", None)
                execution_contract.pop("path_policy_sha256", None)
                execution_contract.pop("path_verifier", None)
            payload["execution_contract"] = execution_contract
            cur.execute(
                "UPDATE engineering_tasks SET execution_contract = ? WHERE id = ?",
                (json.dumps(execution_contract), fixture["task_id"]),
            )
            cur.execute(
                "UPDATE approvals SET payload = ? WHERE id = ?",
                (json.dumps(payload), fixture["approval_id"]),
            )

    response = client.get(f"/api/v2/engineering-tasks/{fixture['task_id']}/patch")

    assert response.status_code == 409
    assert _error_message(response) == "AI Engineering Task patch 完整性驗證失敗"


@pytest.mark.parametrize(
    "status",
    ["queued", "running", "no_changes", "failed", "secret_violation", "path_policy_violation"],
)
def test_only_done_native_results_are_downloadable(api_client, tmp_path, status):
    client, main_module = api_client
    fixture = _create_downloadable_task(main_module, tmp_path)
    main_module.app_state.db.update_coding_run(fixture["run_id"], status=status)

    response = client.get(f"/api/v2/engineering-tasks/{fixture['task_id']}/patch")

    assert response.status_code == 409
    assert _error_message(response) == "AI Engineering Task patch 尚未可下載"


def test_legacy_missing_and_raw_bundle_download_routes_remain_disabled(
    api_client, tmp_path
):
    client, main_module = api_client
    fixture = _create_downloadable_task(main_module, tmp_path)
    db = main_module.app_state.db
    legacy_approval = db.insert_approval(
        "coding_task", {"project": "legacy", "instruction": "legacy"}
    )
    legacy_run = db.insert_coding_run(
        approval_id=legacy_approval,
        project="legacy",
        runner_server="server-a",
        instruction="legacy",
        status="done",
    )

    missing = client.get(f"/api/v2/engineering-tasks/{uuid.uuid4()}/patch")
    legacy = client.get(f"/api/v2/engineering-tasks/legacy-coding-run-{legacy_run}/patch")
    raw_bundle = client.get(f"/api/v2/engineering-tasks/{fixture['task_id']}/bundle")
    generic_download = client.get(
        f"/api/v2/engineering-tasks/{fixture['task_id']}/artifacts/"
        f"{fixture['bundle_artifact_id']}/download"
    )
    bundle_metadata = client.get(
        f"/api/v2/engineering-tasks/{fixture['task_id']}/artifacts/"
        f"{fixture['bundle_artifact_id']}"
    )

    assert missing.status_code == legacy.status_code == 404
    assert _error_message(missing) == _error_message(legacy) == "native AI Engineering Task 不存在"
    #: The legacy artifact metadata/download routes were deleted with the
    #: request surfaces (DG-CONSOLIDATION-v1 C-5 (c)); the raw bundle bytes
    #: are reachable through no route at all.
    assert raw_bundle.status_code == generic_download.status_code == bundle_metadata.status_code == 404
    assert fixture["bundle_path"].read_bytes() not in (raw_bundle.content, generic_download.content, bundle_metadata.content)


@pytest.fixture
def authenticated_patch_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DB_PATH", str(tmp_path / "auth-patch.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "auth-patch-audit.jsonl"))
    monkeypatch.setenv("SERVERS_YAML_PATH", str(tmp_path / "servers.yaml"))
    monkeypatch.setenv("AUTH_TOKEN", "patch-auth-token")
    import app.main as main_module

    with TestClient(main_module.app) as client:
        yield client, main_module


def test_patch_route_uses_existing_authentication_middleware(
    authenticated_patch_client, tmp_path
):
    client, main_module = authenticated_patch_client
    fixture = _create_downloadable_task(main_module, tmp_path)
    route = f"/api/v2/engineering-tasks/{fixture['task_id']}/patch"

    assert client.get(route).status_code == 401
    assert client.get(route, headers={"X-Auth-Token": "wrong"}).status_code == 401
    accepted = client.get(route, headers={"X-Auth-Token": "patch-auth-token"})
    assert accepted.status_code == 200


@pytest.fixture
def shadow_patch_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DB_PATH", str(tmp_path / "shadow-patch.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "shadow-patch-audit.jsonl"))
    monkeypatch.setenv("SERVERS_YAML_PATH", str(tmp_path / "servers.yaml"))
    monkeypatch.setenv("AUTHORIZATION_MODE", "shadow")
    monkeypatch.setenv("AUTH_TOKEN", "")
    import app.main as main_module

    with TestClient(main_module.app) as client:
        yield client, main_module


def test_authorization_shadow_observes_but_does_not_enforce(
    shadow_patch_client, tmp_path
):
    client, main_module = shadow_patch_client
    fixture = _create_downloadable_task(main_module, tmp_path)

    response = client.get(f"/api/v2/engineering-tasks/{fixture['task_id']}/patch")

    assert response.status_code == 200
    records = read_audit(main_module.app_state.config.audit_path)
    observed = [record for record in records if record["action"].startswith("authorization_shadow")]
    assert observed
    assert observed[-1]["params"]["route"] == (
        "GET /api/v2/engineering-tasks/{task_id}/patch"
    )
    assert observed[-1]["params"]["action"] == "project.view"
