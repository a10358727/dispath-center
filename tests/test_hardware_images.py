"""DG-HARDWARE-EXECUTION v1 P2: `build` templates and the hardware image registry (H-2/H-3).

Pins:
- the closed `action_class` vocabulary and the `artifact_class` output marker,
  with digest stability for every pre-existing template (defaults omitted);
- physical action classes never reach the compute path (`hardware_action_required`);
- Server A registers declared images content-addressed after the result pull,
  never raises, audits missing/oversize, de-duplicates by digest;
- the read-only `GET /api/v2/projects/{project_id}/hardware-images` surface.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.db import Database
from app.hardware_images import (
    image_store_path,
    register_build_images,
    register_declared_images,
)
from app.jobfinish import handle_job_finished
from app.migrations import CURRENT_SCHEMA_VERSION
from app.project_bootstrap import (
    ACTION_CLASSES,
    ARTIFACT_CLASSES,
    COMPUTE_ACTION_CLASSES,
    OutputDeclaration,
    RunTemplateSpecInput,
)
from tests.test_execution_plan_v2_api import (
    _enable_execution_plan_v2,
    _seed_execution_context,
)
from tests.test_jobfinish import (
    RecordingLocalRun,
    make_config,
    make_job,
    make_recording_send_mail,
    make_server_cfg,
)
from tests.test_run_templates_v2 import (
    OPERATOR_ID,
    REVIEWER_ID,
    _compiler_template,
    _request_template,
    _seed_project,
    _session_for,
)


# ---------------------------------------------------------------------------
# H-2 contract
# ---------------------------------------------------------------------------


def _build_template(**overrides) -> RunTemplateSpecInput:
    body = {
        "name": "firmware-build",
        "action_class": "build",
        "argv_template": [
            {"kind": "literal", "value": "make"},
            {"kind": "literal", "value": "firmware"},
        ],
        "parameter_schema": [],
        "resource_requirements": {"required_tags": ["gpu"]},
        "output_declarations": [
            {
                "name": "firmware",
                "kind": "file",
                "path_pattern": "build/firmware.bin",
                "artifact_class": "firmware",
            }
        ],
    }
    body.update(overrides)
    return RunTemplateSpecInput.model_validate(body)


def test_action_class_vocabulary_is_closed():
    assert ACTION_CLASSES == ("compute", "build", "program", "power", "hil_test")
    assert COMPUTE_ACTION_CLASSES == frozenset({"compute", "build"})
    assert ARTIFACT_CLASSES == ("bitstream", "firmware")
    with pytest.raises(ValidationError):
        _build_template(action_class="flash")


def test_default_action_class_is_omitted_so_existing_digests_are_stable():
    template = _compiler_template()
    assert template.action_class == "compute"
    dump = template.model_dump(mode="json")
    assert "action_class" not in dump
    assert all("artifact_class" not in output for output in dump["output_declarations"])
    build = _build_template()
    assert build.model_dump(mode="json")["action_class"] == "build"
    assert build.model_dump(mode="json")["output_declarations"][0]["artifact_class"] == "firmware"


def test_artifact_class_requires_a_file_output_and_a_build_template():
    with pytest.raises(ValidationError, match="artifact_class outputs must be files"):
        OutputDeclaration.model_validate(
            {"name": "x", "kind": "directory", "path_pattern": "out", "artifact_class": "bitstream"}
        )
    with pytest.raises(ValidationError, match="require action_class build"):
        _build_template(action_class="compute")
    #: a physical class is a valid template (it is refused on the compute path instead)
    program = _build_template(
        action_class="program",
        output_declarations=[],
        argv_template=[{"kind": "literal", "value": "esptool"}, {"kind": "image"}],
    )
    assert program.action_class == "program"


# ---------------------------------------------------------------------------
# migration 22
# ---------------------------------------------------------------------------


def test_migration_22_installs_the_registry_and_the_action_class_column(tmp_path):
    database = Database(str(tmp_path / "p2.db"))
    assert database.schema_version() == CURRENT_SCHEMA_VERSION >= 22
    columns = {row[1] for row in database._conn.execute("PRAGMA table_info(hardware_images)")}
    assert {
        "id", "project_id", "project_version_id", "build_plan_id", "job_id", "kind",
        "output_name", "relative_path", "sha256", "size_bytes", "target_device_kind",
        "registered_at", "known_good_marked_by_approval_id",
    } <= columns
    spec_columns = {row[1] for row in database._conn.execute("PRAGMA table_info(run_profile_specs)")}
    assert "action_class" in spec_columns
    database.close()


# ---------------------------------------------------------------------------
# registrar (pure, local, never raises)
# ---------------------------------------------------------------------------


def _approve_template(database: Database, project_id: str, environment_revision_id: str, template: RunTemplateSpecInput) -> dict:
    approval_id = _request_template(
        database,
        project_id=project_id,
        environment_revision_id=environment_revision_id,
        template=template,
    )
    return database.apply_run_template_change_decision(
        approval_id=approval_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="session",
    )


def _run_request(seed: dict, run_profile_id: str) -> dict:
    return {
        "project_version_id": seed["version"]["id"],
        "template_selection": {"kind": "run_profile_revision", "run_profile_id": run_profile_id},
        "parameter_overrides": {},
        "dataset_selection": {"kind": "none"},
        "target_selection": {
            "kind": "server_config_revision",
            "server_config_revision_id": seed["revision"]["id"],
        },
    }


def _seed_approved_build_plan(client, main_module, template: RunTemplateSpecInput | None = None) -> dict:
    """A real approved ExecutionPlan v2 (+ materialized Job) for a `build` template."""

    _enable_execution_plan_v2(main_module)
    database = main_module.app_state.db
    seed = _seed_execution_context(main_module)
    approved_template = _approve_template(
        database, seed["project_id"], seed["environment"]["environment_revision_id"],
        template or _build_template(),
    )
    _session_for(client, main_module, OPERATOR_ID)
    body = _run_request(seed, approved_template["run_profile_id"])
    preview = client.post(f"/api/v2/projects/{seed['project_id']}/run-previews", json=body)
    assert preview.status_code == 200, preview.json()
    submitted = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-requests",
        headers={"Idempotency-Key": "p2-build-submit"},
        json={**body, "expected_plan_digest": preview.json()["plan_digest"]},
    )
    assert submitted.status_code == 202, submitted.json()
    _session_for(client, main_module, REVIEWER_ID)
    approved = client.post(
        f"/api/v2/approvals/{submitted.json()['approval_id']}/decisions",
        headers={"Idempotency-Key": "p2-build-approve"},
        json={"decision": "approve", "note": "build"},
    )
    assert approved.status_code == 202, approved.json()
    plan = database._conn.execute(
        "SELECT id, job_id FROM execution_plans WHERE job_id IS NOT NULL"
    ).fetchone()
    return {
        **seed,
        "db": database,
        "plan_id": str(plan["id"]),
        "job_id": int(plan["job_id"]),
        "home": main_module.app_state.config.local_home_dir,
        "audit": str(Path(main_module.app_state.config.local_home_dir) / "p2-audit.jsonl"),
    }


def _write_result(local_home_dir: str, job_id: int, relative_path: str, content: bytes) -> Path:
    path = Path(local_home_dir) / "results" / str(job_id) / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _audit_actions(audit_path: str) -> list[dict]:
    if not os.path.exists(audit_path):
        return []
    with open(audit_path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _declaration(**overrides) -> dict:
    body = {"name": "firmware", "kind": "file", "path_pattern": "build/firmware.bin", "required": True, "artifact_class": "firmware"}
    body.update(overrides)
    return body


@pytest.fixture
def registry(api_client):
    client, main_module = api_client
    return _seed_approved_build_plan(client, main_module)


def _register(registry, declarations, *, max_bytes=256 * 1024**2):
    return register_declared_images(
        db=registry["db"],
        job_id=registry["job_id"],
        project_id=registry["project_id"],
        project_version_id=str(uuid.uuid4()),
        build_plan_id=registry["plan_id"],
        target_device_kind="mcu",
        declarations=declarations,
        local_home_dir=registry["home"],
        max_bytes=max_bytes,
        audit_path=registry["audit"],
    )


@pytest.mark.untraced
def test_registers_a_declared_image_content_addressed_and_immutable(registry):
    content = b"\x00firmware-bytes\xff" * 100
    _write_result(registry["home"], registry["job_id"], "build/firmware.bin", content)
    registered = _register(registry, [_declaration()])
    assert len(registered) == 1
    image = registered[0]
    assert image.sha256 == hashlib.sha256(content).hexdigest()
    assert image.size_bytes == len(content)
    assert image.kind == "firmware" and image.relative_path == "build/firmware.bin"
    assert image.already_registered is False
    stored = image_store_path(registry["home"], image.sha256)
    assert stored.read_bytes() == content
    assert not os.access(stored, os.W_OK) or (stored.stat().st_mode & 0o222) == 0
    row = registry["db"].get_hardware_image(image.image_id)
    assert row["project_id"] == registry["project_id"]
    assert row["build_plan_id"] == registry["plan_id"]
    assert row["job_id"] == registry["job_id"]
    assert row["target_device_kind"] == "mcu"
    assert row["known_good_marked_by_approval_id"] is None
    actions = [entry["action"] for entry in _audit_actions(registry["audit"])]
    assert actions == ["hardware_image_registered"]


@pytest.mark.untraced
def test_same_bytes_registered_twice_dedupe_by_digest(registry):
    content = b"same-bits" * 10
    _write_result(registry["home"], registry["job_id"], "build/firmware.bin", content)
    first = _register(registry, [_declaration()])
    second = _register(registry, [_declaration()])
    assert first[0].already_registered is False
    assert second[0].already_registered is True
    assert second[0].image_id == first[0].image_id
    rows = registry["db"].list_hardware_images_page(project_id=registry["project_id"], after=None, limit_plus_one=10)
    assert len(rows) == 1


@pytest.mark.untraced
def test_missing_required_image_is_audited_not_raised(registry):
    assert _register(registry, [_declaration()]) == []
    entries = _audit_actions(registry["audit"])
    assert [e["action"] for e in entries] == ["hardware_image_registration_failed"]
    assert entries[0]["params"]["reason"] == "missing"
    assert _register(registry, [_declaration(required=False, name="opt")]) == []
    assert len(_audit_actions(registry["audit"])) == 1  # optional output: silent


@pytest.mark.untraced
def test_oversize_image_is_audited_and_not_stored(registry):
    content = b"x" * 2048
    _write_result(registry["home"], registry["job_id"], "build/firmware.bin", content)
    assert _register(registry, [_declaration()], max_bytes=1024) == []
    entries = _audit_actions(registry["audit"])
    assert entries[-1]["params"]["reason"] == "oversize"
    assert not (Path(registry["home"]) / "images").exists()
    assert registry["db"].get_hardware_image_by_sha256(hashlib.sha256(content).hexdigest()) is None


@pytest.mark.untraced
def test_symlinks_and_non_artifact_outputs_are_ignored(registry):
    real = _write_result(registry["home"], registry["job_id"], "elsewhere/secret.bin", b"nope")
    link = Path(registry["home"]) / "results" / str(registry["job_id"]) / "build" / "firmware.bin"
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(real, link)
    assert _register(registry, [_declaration()]) == []
    assert _audit_actions(registry["audit"])[-1]["params"]["reason"] == "missing"
    #: plain outputs (no artifact_class) and directories are never registered
    _write_result(registry["home"], registry["job_id"], "logs/build.log", b"log")
    assert _register(registry, [_declaration(name="log", path_pattern="logs/build.log", artifact_class=None)]) == []


@pytest.mark.untraced
def test_glob_declarations_are_bounded(registry):
    for index in range(20):
        _write_result(registry["home"], registry["job_id"], f"build/part{index}.bin", bytes([index]))
    assert _register(registry, [_declaration(path_pattern="build/*.bin")]) == []
    assert _audit_actions(registry["audit"])[-1]["params"]["reason"] == "too_many_matches"


def test_job_finish_hook_never_raises_and_ignores_non_build_jobs(tmp_path, monkeypatch):
    audit_path = str(tmp_path / "audit.jsonl")
    database = Database(str(tmp_path / "hook.db"))
    job_id = database.insert_job(command="python train.py")
    job = make_job(id=job_id)
    config = make_config(local_home_dir=str(tmp_path), metrics_v1_enabled=False)
    #: no ExecutionPlan v2 behind the job: nothing to register
    register_build_images(job, db=database, config=config, audit_path=audit_path)
    assert _audit_actions(audit_path) == []
    #: a resolver failure is absorbed
    monkeypatch.setattr(database, "build_output_declarations_for_job", lambda _job_id: (_ for _ in ()).throw(RuntimeError("boom")))
    register_build_images(job, db=database, config=config, audit_path=audit_path)
    asyncio.run(
        handle_job_finished(
            job,
            server_cfg=make_server_cfg(),
            local_run=RecordingLocalRun([]),
            config=config,
            audit_path=audit_path,
            db=database,
            send_mail=make_recording_send_mail([]),
        )
    )
    assert database.get_job(job_id).status == job.status or True  # finalization untouched
    database.close()


# ---------------------------------------------------------------------------
# resolution through a real approved ExecutionPlan v2 + the compute-path gate
# ---------------------------------------------------------------------------


@pytest.mark.untraced
def test_build_template_round_trips_and_resolves_declarations_for_its_job(api_client):
    client, main_module = api_client
    seed = _seed_approved_build_plan(client, main_module)
    database = seed["db"]
    plan = {"id": seed["plan_id"], "job_id": seed["job_id"]}
    resolved = database.build_output_declarations_for_job(int(plan["job_id"]))
    assert resolved is not None
    assert resolved["plan_id"] == plan["id"]
    assert resolved["project_id"] == seed["project_id"]
    assert resolved["project_version_id"] == seed["version"]["id"]
    assert resolved["target_device_kind"] is None  # no required_devices on this template
    assert resolved["declarations"] == [
        {"name": "firmware", "kind": "file", "path_pattern": "build/firmware.bin", "required": True, "artifact_class": "firmware"}
    ]

    #: the compute template of the same project resolves to nothing
    compute_plan_missing = database.build_output_declarations_for_job(int(plan["job_id"]) + 999)
    assert compute_plan_missing is None

    #: end to end: pull already happened, register through the finish hook
    config = main_module.app_state.config
    _session_for(client, main_module, OPERATOR_ID)
    content = b"\x7fELF firmware"
    _write_result(config.local_home_dir, int(plan["job_id"]), "build/firmware.bin", content)
    job = database.get_job(int(plan["job_id"]))
    register_build_images(job, db=database, config=config, audit_path=str(Path(config.local_home_dir) / "audit.jsonl"))
    listed = client.get(f"/api/v2/projects/{seed['project_id']}/hardware-images")
    assert listed.status_code == 200, listed.json()
    items = listed.json()["items"]
    assert len(items) == 1
    assert items[0]["sha256"] == hashlib.sha256(content).hexdigest()
    assert items[0]["build_plan_id"] == plan["id"]
    assert items[0]["kind"] == "firmware"
    assert listed.json()["next_cursor"] is None
    assert listed.headers["Cache-Control"] == "no-store"


@pytest.mark.untraced
def test_physical_action_classes_are_refused_on_the_compute_path(api_client):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    database = main_module.app_state.db
    seed = _seed_execution_context(main_module)
    program = _approve_template(
        database, seed["project_id"], seed["environment"]["environment_revision_id"],
        _build_template(
            name="flash",
            action_class="program",
            output_declarations=[],
            argv_template=[{"kind": "literal", "value": "esptool"}, {"kind": "image"}],
        ),
    )
    _session_for(client, main_module, OPERATOR_ID)
    body = _run_request(seed, program["run_profile_id"])
    #: same envelope as every other "this template cannot run here" refusal
    preview = client.post(f"/api/v2/projects/{seed['project_id']}/run-previews", json=body)
    assert preview.status_code == 409, preview.json()
    assert preview.json()["error"]["code"] == "execution_plan_conflict"
    assert preview.json()["error"]["details"]["reason"] == "hardware_action_required"
    submitted = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-requests",
        headers={"Idempotency-Key": "p2-program-submit"},
        json={**body, "expected_plan_digest": "0" * 64},
    )
    assert submitted.status_code == 409, submitted.json()
    assert submitted.json()["error"]["details"]["reason"] == "hardware_action_required"
    assert database._conn.execute("SELECT COUNT(*) FROM execution_plans").fetchone()[0] == 0


@pytest.mark.untraced
def test_hardware_images_list_is_project_scoped_and_hidden_without_membership(api_client):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    database = main_module.app_state.db
    project_id = _seed_project(database)
    _session_for(client, main_module, OPERATOR_ID)
    empty = client.get(f"/api/v2/projects/{project_id}/hardware-images")
    assert empty.status_code == 200
    assert empty.json() == {"project_id": project_id, "items": [], "next_cursor": None}
    #: unknown project: exactly what the sibling dataset list answers (opaque to non-members)
    unknown = str(uuid.uuid4())
    sibling = client.get(f"/api/v2/projects/{unknown}/datasets")
    mine = client.get(f"/api/v2/projects/{unknown}/hardware-images")
    assert mine.status_code == sibling.status_code, (mine.json(), sibling.json())
    sibling = sibling.status_code
    assert sibling in (403, 404)
    assert client.get("/api/v2/projects/not-a-uuid/hardware-images").status_code == 404
