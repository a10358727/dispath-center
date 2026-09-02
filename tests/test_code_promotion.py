"""DG-CODE-PROMOTE-v1 native Engineering Task promotion tests.

These tests use a real Git repository, a real bundle and a real local bare Hub.
No network, GitHub or production credential is involved.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.approvals import (
    CodePromotionDisabledError,
    InvalidCodePromotionRequestError,
    approve,
    maybe_auto_approve,
    request_engineering_task_promote_approval,
)
from app.code_promotion import PromotionPublishError
from app.config import AppConfig
from app.db import VALID_APPROVAL_KINDS, Database
from app.execution_plan import PlanInputs, derive_plan_draft
from app.localrun import local_run
from app.results import local_result_dir
from app.sshpool import CommandResult


@pytest.fixture()
def database(tmp_path):
    db = Database(str(tmp_path / "promote.db"))
    yield db
    db.close()


def _git(*args: str, cwd: Path | None = None, check: bool = True):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
    )


def _enabled_config(home: Path) -> AppConfig:
    return AppConfig(
        servers=[],
        local_home_dir=str(home),
        code_promotion_v1_enabled=True,
    )


class _State:
    def __init__(self, config: AppConfig):
        self.config = config


@dataclass(frozen=True)
class _Seed:
    task_id: str
    project_id: str
    base_version_id: str
    base_commit: str
    result_commit: str
    job_id: int
    coding_run_id: int
    bundle_path: Path
    bundle_sha256: str
    hub_path: Path


def _seed_native_candidate(
    db: Database,
    home: Path,
    *,
    project: str = "demo",
    valid_bundle: bool = True,
) -> _Seed:
    """Create the durable native task/run/artifact chain promotion requires."""

    work = home / f"{project}-work"
    hub = home / "git" / f"{project}.git"
    work.mkdir(parents=True)
    _git("init", cwd=work)
    _git("checkout", "-b", "main", cwd=work)
    _git("config", "user.name", "Promotion Test", cwd=work)
    _git("config", "user.email", "promotion@example.invalid", cwd=work)
    (work / "README.md").write_text("base\n", encoding="utf-8")
    _git("add", "README.md", cwd=work)
    _git("commit", "-m", "base", cwd=work)
    base_commit = _git("rev-parse", "HEAD", cwd=work).stdout.strip()

    hub.parent.mkdir(parents=True)
    _git("clone", "--bare", str(work), str(hub))

    _git("checkout", "-b", "codex-result", cwd=work)
    (work / "README.md").write_text("base\ncodex change\n", encoding="utf-8")
    _git("add", "README.md", cwd=work)
    _git("commit", "-m", "codex result", cwd=work)
    result_commit = _git("rev-parse", "HEAD", cwd=work).stdout.strip()

    project_id = db.insert_project(project, str(work))
    base_version = db.get_or_create_project_version(
        project,
        base_commit,
        git_ref="refs/heads/main",
    )

    task_id = str(uuid.uuid4())
    task_id, parent_approval_id = db.insert_engineering_task_request(
        task_id=task_id,
        project_id=project_id,
        project_name=project,
        project_version_id=base_version.id,
        base_commit=base_commit,
        agent_provider_id="codex",
        provider_capabilities={},
        execution_contract={},
        contract_version="engineering-task-v2",
        structured_request={},
        instruction="Make the reviewed change",
        detected_metadata={},
        runner_server="runner-a",
        validation_target=None,
        approval_payload={"engineering_task_id": task_id},
    )
    db.update_approval(
        parent_approval_id,
        status="approved",
        decision_mechanism="human",
    )
    job_id = db.insert_job(
        command="true",
        type="coding",
        project=project,
        pin_server="runner-a",
        status="done",
        engineering_task_id=task_id,
        engineering_task_role="coding",
        engineering_attempt_number=1,
    )
    bundle_path = (
        Path(local_result_dir(job_id, str(home))) / "changes.bundle"
    )
    bundle_path.parent.mkdir(parents=True)
    if valid_bundle:
        _git(
            "bundle",
            "create",
            str(bundle_path),
            "refs/heads/codex-result",
            f"^{base_commit}",
            cwd=work,
        )
    else:
        bundle_path.write_bytes(b"not-a-git-bundle")

    coding_run_id = db.insert_coding_run(
        approval_id=parent_approval_id,
        job_id=job_id,
        project=project,
        runner_server="runner-a",
        instruction="Make the reviewed change",
        base_commit=base_commit,
        result_commit=result_commit,
        bundle_path=str(bundle_path),
        status="done",
        engineering_task_id=task_id,
        project_version_id=base_version.id,
        base_binding="project_version_pinned",
        attempt_number=1,
    )
    db.update_engineering_task(
        task_id,
        coding_run_id=coding_run_id,
        status="done",
    )
    artifact = db.register_engineering_task_artifact(
        task_id=task_id,
        attempt_number=1,
        artifact_key="bundle",
        kind="bundle",
        label="changes.bundle",
        storage_kind="local_result",
        coding_run_id=coding_run_id,
        source_job_id=job_id,
        storage_key="changes.bundle",
        content_type="application/x-git-bundle",
    )
    bundle_bytes = bundle_path.read_bytes()
    bundle_sha256 = hashlib.sha256(bundle_bytes).hexdigest()
    db.record_engineering_task_artifact_collection(
        artifact.id,
        source_sha256=bundle_sha256,
        source_size_bytes=len(bundle_bytes),
        verification_status="verified",
        redaction_status="not_applicable",
        availability="available",
    )
    return _Seed(
        task_id=task_id,
        project_id=project_id,
        base_version_id=base_version.id,
        base_commit=base_commit,
        result_commit=result_commit,
        job_id=job_id,
        coding_run_id=coding_run_id,
        bundle_path=bundle_path,
        bundle_sha256=bundle_sha256,
        hub_path=hub,
    )


def _payload(**overrides):
    payload = {
        "engineering_task_id": str(uuid.uuid4()),
        "project_name": "demo",
        "base_project_version_id": str(uuid.uuid4()),
        "git_commit": "a" * 40,
        "bundle_sha256": "b" * 64,
    }
    payload.update(overrides)
    return payload


def _request(db: Database, home: Path, seed: _Seed):
    return request_engineering_task_promote_approval(
        db,
        seed.task_id,
        config=_enabled_config(home),
        audit_path="/dev/null",
    )


def _approve(
    db: Database,
    home: Path,
    approval_id: int,
    *,
    runner=local_run,
    approved_by: str = "human",
):
    return asyncio.run(
        approve(
            db,
            approval_id,
            app_state=_State(_enabled_config(home)),
            approved_by=approved_by,
            local_run=runner,
            audit_path="/dev/null",
        )
    )


def _promotion_rows(db: Database) -> list[dict]:
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM project_versions"
            " WHERE promotion_approval_id IS NOT NULL ORDER BY created_at"
        )
        return [dict(row) for row in cursor.fetchall()]


# ---------------------------------------------------------------------------
# Pinned contract and rollout boundary
# ---------------------------------------------------------------------------


def test_payload_carries_only_identifiers_and_digests(database):
    assert database.insert_pinned_approval(
        kind="engineering_task_promote",
        contract_version="code-promotion-v1",
        payload=_payload(),
    ) > 0

    for extra in (
        {"command": "make deploy"},
        {"bundle_path": "/tmp/x"},
        {"branch": "main"},
    ):
        with pytest.raises(ValueError, match="invalid code promotion contract"):
            database.insert_pinned_approval(
                kind="engineering_task_promote",
                contract_version="code-promotion-v1",
                payload={**_payload(), **extra},
            )


@pytest.mark.parametrize(
    "bad",
    [
        {"engineering_task_id": "task-1"},
        {"base_project_version_id": None},
        {"git_commit": "not-a-commit"},
        {"git_commit": "a" * 39},
        {"git_commit": "A" * 40},
        {"bundle_sha256": "short"},
    ],
)
def test_malformed_promotion_identity_is_rejected(database, bad):
    with pytest.raises(ValueError, match="invalid code promotion contract"):
        database.insert_pinned_approval(
            kind="engineering_task_promote",
            contract_version="code-promotion-v1",
            payload={**_payload(), **bad},
        )


def test_promotion_is_registered_but_never_auto_approved():
    assert "engineering_task_promote" in VALID_APPROVAL_KINDS
    source = inspect.getsource(maybe_auto_approve)
    assert 'if approval.kind not in ("enqueue", "stop"):' in source
    assert "engineering_task_promote" not in source


@pytest.mark.usefixtures("legacy_posture")
def test_rollout_flag_off_makes_the_request_fail_closed(
    database, tmp_path
):
    # 整頓 C6: the flag defaults on (pilot posture); switching it off must
    # still fail closed with zero writes.
    seed = _seed_native_candidate(database, tmp_path)
    assert AppConfig(servers=[]).code_promotion_v1_enabled is True
    with pytest.raises(CodePromotionDisabledError):
        request_engineering_task_promote_approval(
            database,
            seed.task_id,
            config=AppConfig(servers=[], local_home_dir=str(tmp_path), code_promotion_v1_enabled=False),
            audit_path="/dev/null",
        )
    assert database.list_approvals(kind="engineering_task_promote") == []


def test_request_pins_native_candidate_without_publishing(database, tmp_path):
    seed = _seed_native_candidate(database, tmp_path)
    approval = _request(database, tmp_path, seed)

    assert approval.status == "pending"
    assert approval.payload_contract_version == "code-promotion-v1"
    assert approval.payload == {
        "engineering_task_id": seed.task_id,
        "project_name": "demo",
        "base_project_version_id": seed.base_version_id,
        "git_commit": seed.result_commit,
        "bundle_sha256": seed.bundle_sha256,
    }
    assert _git(
        "--git-dir",
        str(seed.hub_path),
        "show-ref",
        "--verify",
        "refs/heads/codex-promoted/does-not-exist",
        check=False,
    ).returncode != 0
    assert _promotion_rows(database) == []


def test_public_request_route_is_flagged_and_only_creates_pending_approval(
    api_client, tmp_path
):
    client, main_module = api_client
    main_module.app_state.config.local_home_dir = str(tmp_path)
    main_module.app_state.config.code_promotion_v1_enabled = True
    seed = _seed_native_candidate(main_module.app_state.db, tmp_path)

    response = client.post(
        f"/engineering-tasks/{seed.task_id}/promote-request"
    )

    assert response.status_code == 200, response.json()
    assert response.json()["approval"]["status"] == "pending"
    assert _promotion_rows(main_module.app_state.db) == []


@pytest.mark.usefixtures("legacy_posture")
def test_public_request_route_is_not_exposed_when_flag_is_off(api_client):
    client, main_module = api_client
    assert main_module.app_state.config.code_promotion_v1_enabled is False
    response = client.post(
        f"/engineering-tasks/{uuid.uuid4()}/promote-request"
    )
    assert response.status_code == 404


@pytest.mark.parametrize("status", ["running", "discarded"])
def test_nonterminal_or_discarded_task_cannot_request_promotion(
    database, tmp_path, status
):
    seed = _seed_native_candidate(database, tmp_path)
    database.update_engineering_task(seed.task_id, status=status)
    with pytest.raises(InvalidCodePromotionRequestError):
        _request(database, tmp_path, seed)
    assert database.list_approvals(kind="engineering_task_promote") == []


# ---------------------------------------------------------------------------
# Real bundle verification and Hub publication
# ---------------------------------------------------------------------------


def test_approval_publishes_exact_hub_ref_and_immutable_provenance(
    database, tmp_path
):
    seed = _seed_native_candidate(database, tmp_path)
    approval = _request(database, tmp_path, seed)
    result = _approve(database, tmp_path, approval.id)

    version = result["project_version"]
    assert result["approval"].status == "approved"
    assert version["promotion_state"] == "promoted"
    assert version["promotion_approval_id"] == approval.id
    assert version["bundle_sha256"] == seed.bundle_sha256
    assert version["promoted_at"]
    assert database.project_version_is_promoted(version["id"]) is True
    promotion_events = {
        event["action"]
        for event in database.list_durable_audit_events(limit=100)
        if event["resource_type"] == "project_version"
        and event["resource_id"] == version["id"]
    }
    assert {
        "engineering_task_promotion_prepared",
        "engineering_task_promoted",
    } <= promotion_events
    assert (
        _git(
            "--git-dir",
            str(seed.hub_path),
            "rev-parse",
            f"{version['git_ref']}^{{commit}}",
        ).stdout.strip()
        == seed.result_commit
    )

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with database.cursor() as cursor:
            cursor.execute(
                "UPDATE project_versions SET bundle_sha256 = ? WHERE id = ?",
                ("f" * 64, version["id"]),
            )
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        with database.cursor() as cursor:
            cursor.execute(
                "DELETE FROM project_versions WHERE id = ?", (version["id"],)
            )
    with pytest.raises(sqlite3.IntegrityError, match="referenced"):
        with database.cursor() as cursor:
            cursor.execute(
                "DELETE FROM approvals WHERE id = ?", (approval.id,)
            )


def test_regenerated_bundle_is_rejected_before_import(database, tmp_path):
    seed = _seed_native_candidate(database, tmp_path)
    approval = _request(database, tmp_path, seed)
    seed.bundle_path.write_bytes(seed.bundle_path.read_bytes() + b"tampered")

    result = _approve(database, tmp_path, approval.id)

    assert result["approval"].status == "rejected"
    assert "project_version" not in result
    assert _promotion_rows(database) == []


def test_verified_digest_cannot_substitute_for_git_bundle_verify(
    database, tmp_path
):
    seed = _seed_native_candidate(
        database, tmp_path, valid_bundle=False
    )
    approval = _request(database, tmp_path, seed)

    result = _approve(database, tmp_path, approval.id)

    assert result["approval"].status == "rejected"
    assert "bundle_verify_failed" in (result["approval"].note or "")
    assert _promotion_rows(database) == []


def test_missing_base_version_rejects_without_request(database, tmp_path):
    seed = _seed_native_candidate(database, tmp_path)
    with database.cursor() as cursor:
        cursor.execute(
            "DELETE FROM project_versions WHERE id = ?",
            (seed.base_version_id,),
        )
    with pytest.raises(InvalidCodePromotionRequestError):
        _request(database, tmp_path, seed)
    assert _promotion_rows(database) == []


def test_publish_interruption_leaves_nonrunnable_row_and_retry_converges(
    database, tmp_path
):
    seed = _seed_native_candidate(database, tmp_path)
    approval = _request(database, tmp_path, seed)

    async def fail_ref_publish(command, timeout):
        if " update-ref " in command:
            return CommandResult(1, "", "injected update-ref failure")
        return await local_run(command, timeout)

    with pytest.raises(PromotionPublishError, match="hub_ref_publish_failed"):
        _approve(
            database,
            tmp_path,
            approval.id,
            runner=fail_ref_publish,
        )

    prepared = _promotion_rows(database)
    assert len(prepared) == 1
    assert prepared[0]["promotion_state"] is None
    assert database.project_version_is_promoted(prepared[0]["id"]) is False
    assert database.get_approval(approval.id).status == "pending"
    assert _git(
        "--git-dir",
        str(seed.hub_path),
        "show-ref",
        "--verify",
        prepared[0]["git_ref"],
        check=False,
    ).returncode != 0

    retried = _approve(database, tmp_path, approval.id)
    assert retried["approval"].status == "approved"
    assert retried["project_version"]["id"] == prepared[0]["id"]
    assert database.project_version_is_promoted(prepared[0]["id"]) is True


def test_promoting_same_commit_twice_is_idempotent(database, tmp_path):
    seed = _seed_native_candidate(database, tmp_path)
    first_approval = _request(database, tmp_path, seed)
    first = _approve(database, tmp_path, first_approval.id)["project_version"]

    second_approval = _request(database, tmp_path, seed)
    second = _approve(
        database, tmp_path, second_approval.id
    )["project_version"]

    assert first["id"] == second["id"]
    assert len(_promotion_rows(database)) == 1
    assert database.get_approval(second_approval.id).status == "approved"


def test_direct_or_automatic_approval_shape_is_rejected(database, tmp_path):
    seed = _seed_native_candidate(database, tmp_path)
    approval = _request(database, tmp_path, seed)

    result = _approve(
        database,
        tmp_path,
        approval.id,
        approved_by="web-direct",
    )

    assert result["approval"].status == "rejected"
    assert "manual human" in (result["approval"].note or "")
    assert _promotion_rows(database) == []


def test_retirement_is_rollback_without_evidence_deletion(database, tmp_path):
    seed = _seed_native_candidate(database, tmp_path)
    approval = _request(database, tmp_path, seed)
    version = _approve(
        database, tmp_path, approval.id
    )["project_version"]

    retired = database.retire_project_version_promotion(version["id"])

    assert retired["promotion_state"] == "retired"
    assert database.project_version_is_promoted(version["id"]) is False
    retired_events = [
        event
        for event in database.list_durable_audit_events(limit=100)
        if event["action"] == "engineering_task_promotion_retired"
        and event["resource_id"] == version["id"]
    ]
    assert len(retired_events) == 1
    assert (
        _git(
            "--git-dir",
            str(seed.hub_path),
            "rev-parse",
            f"{version['git_ref']}^{{commit}}",
        ).stdout.strip()
        == seed.result_commit
    )


# ---------------------------------------------------------------------------
# Planner gate and migration honesty
# ---------------------------------------------------------------------------


def test_legacy_version_cannot_back_reproducible_plan(database):
    with database.cursor() as cursor:
        cursor.execute(
            "INSERT INTO project_versions"
            " (id, project_name, git_commit, created_at)"
            " VALUES ('legacy-2', 'demo', ?, 'legacy')",
            ("d" * 40,),
        )
    inputs = PlanInputs(
        project_name="demo",
        command="python train.py",
        project_version_id="legacy-2",
        run_profile_id="rp-1",
        dataset_none=True,
        server_config_revision_id="rev-1",
    )
    draft = derive_plan_draft(
        inputs, database.resolve_execution_plan_inputs(inputs)
    )
    assert "project_version_missing" in draft.reason_codes
    assert draft.reproducible is False


def test_promoted_version_satisfies_plan_resolver(database, tmp_path):
    seed = _seed_native_candidate(database, tmp_path)
    approval = _request(database, tmp_path, seed)
    version = _approve(
        database, tmp_path, approval.id
    )["project_version"]
    inputs = PlanInputs(
        project_name="demo",
        command="python train.py",
        project_version_id=version["id"],
        dataset_none=True,
    )
    assert (
        database.resolve_execution_plan_inputs(
            inputs
        ).project_version_exists
        is True
    )


def test_migration_never_backdates_legacy_version(tmp_path):
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(path))
    raw.executescript(
        """
        CREATE TABLE project_versions (
            id TEXT PRIMARY KEY, project_id TEXT, project_name TEXT NOT NULL,
            git_commit TEXT NOT NULL, git_ref TEXT, source_instance_id TEXT,
            created_at TEXT NOT NULL, metadata TEXT
        );
        INSERT INTO project_versions (id, project_name, git_commit, created_at)
        VALUES ('old', 'demo', 'deadbeef', 'legacy');
        """
    )
    raw.commit()
    raw.close()

    migrated = Database(str(path))
    try:
        assert migrated.project_version_is_promoted("old") is False
        with migrated.cursor() as cursor:
            cursor.execute(
                "SELECT promotion_approval_id, promotion_state"
                " FROM project_versions WHERE id = 'old'"
            )
            assert tuple(cursor.fetchone()) == (None, None)
    finally:
        migrated.close()
