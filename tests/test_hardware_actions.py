"""DG-HARDWARE-EXECUTION v1 P3a: `hardware_action_v2` — physical actions on a board.

Pins (H-2/H-3/H-6):
- a `program` template carries exactly one `image` argv token; the compiled argv
  and the bridge pin the worker image path and its digest;
- the physical path resolves the same immutable ExecutionPlan v2 as a run, plus
  the exact device (declared + present) and a registered image, and files a
  `hardware_action_v2` card: transaction-only, high-risk, never auto-approved;
- the decision re-verifies the stored bytes, pushes them by SFTP (the worker
  never fetches), then materializes the Job; any failure leaves the card pending;
- every refusal is a named, safe conflict reason.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

import tests.test_project_environments_v1 as env_tests
from app import approvals as approval_module
from app.approval_presentation import describe_approval
from app.approvals import maybe_auto_approve
from app.config import ServerConfig
from app.db import TRANSACTION_ONLY_APPROVAL_KINDS, VALID_APPROVAL_KINDS, Database
from app.execution_plan_v2 import build_bash_argv_bridge
from app.hardware_actions import (
    HARDWARE_ACTION_V2_APPROVAL_KIND,
    HardwareActionV2ApprovalPayload,
    image_preflight_lines,
    worker_image_path,
)
from app.hardware_images import image_store_path, register_build_images
from app.project_bootstrap import RunTemplateSpecInput
from app.run_templates import compile_structured_argv
from app.server_attempt_preflight import ATTEMPT_FILESYSTEM_PREFLIGHT_CONTRACT_VERSION
from tests.test_hardware_images import (
    _approve_template,
    _build_template,
    _seed_approved_build_plan,
    _write_result,
)
from tests.test_run_templates_v2 import OPERATOR_ID, REVIEWER_ID, _session_for

DEVICE_ID = "esp32-1"
BOARD_SERVER = "board-118"
BOARD_CHECKOUT = "/srv/projects/product-v2-board"


# ---------------------------------------------------------------------------
# contract
# ---------------------------------------------------------------------------


def _program_template(**overrides) -> RunTemplateSpecInput:
    body = {
        "name": "flash",
        "action_class": "program",
        "argv_template": [
            {"kind": "literal", "value": "esptool"},
            {"kind": "literal", "value": "write_flash"},
            {"kind": "image"},
        ],
        "parameter_schema": [],
        "resource_requirements": {"required_tags": ["gpu"], "required_devices": [{"kind": "mcu"}]},
        "output_declarations": [],
    }
    body.update(overrides)
    return RunTemplateSpecInput.model_validate(body)


def test_image_token_is_closed_and_bound_to_program_templates():
    template = _program_template()
    assert [token.kind for token in template.argv_template] == ["literal", "literal", "image"]
    with pytest.raises(ValidationError, match="exactly one image argv token"):
        _program_template(argv_template=[{"kind": "literal", "value": "esptool"}])
    with pytest.raises(ValidationError, match="exactly one image argv token"):
        _program_template(argv_template=[{"kind": "literal", "value": "esptool"}, {"kind": "image"}, {"kind": "image"}])
    with pytest.raises(ValidationError, match="requires action_class program"):
        _build_template(argv_template=[{"kind": "literal", "value": "make"}, {"kind": "image"}])
    with pytest.raises(ValidationError, match="carries no value or name"):
        _program_template(argv_template=[{"kind": "literal", "value": "x"}, {"kind": "image", "value": "y"}])


def test_image_token_compiles_only_to_an_absolute_server_derived_path():
    template = _program_template()
    with pytest.raises(ValueError, match="absolute image path"):
        compile_structured_argv(template, {})
    with pytest.raises(ValueError, match="absolute image path"):
        compile_structured_argv(template, {}, image_path="relative/image.bin")
    path = worker_image_path(BOARD_CHECKOUT, "a" * 64)
    assert path == "/srv/projects/.dispatch-images/" + "a" * 64 + ".bin"
    compiled = compile_structured_argv(template, {}, image_path=path)
    assert compiled.argv == ("esptool", "write_flash", path)
    with pytest.raises(ValueError):
        worker_image_path("relative/checkout", "a" * 64)


def test_bridge_preflight_lines_guard_the_pinned_digest_before_exec():
    path = worker_image_path(BOARD_CHECKOUT, "b" * 64)
    lines = image_preflight_lines(path, "b" * 64)
    command = build_bash_argv_bridge(
        checkout_path=BOARD_CHECKOUT,
        git_commit="c" * 40,
        setup_command="",
        argv=("esptool", "write_flash", path),
        preflight_lines=lines,
    )
    body = command.split("\n")
    assert body[0] == "set -euo pipefail"
    assert f"test -f {path}" in body
    assert any(line.startswith('test "$(sha256sum -- ') and line.endswith(f"= {'b' * 64}") for line in body)
    assert body.index(f"test -f {path}") < body.index("exec esptool write_flash " + path)
    with pytest.raises(ValueError, match="single non-empty lines"):
        build_bash_argv_bridge(
            checkout_path=BOARD_CHECKOUT, git_commit="c" * 40, setup_command="",
            argv=("x",), preflight_lines=("a\nb",),
        )


def test_approval_payload_shape_is_closed_per_action_class():
    base = {
        "execution_plan_id": str(uuid.uuid4()), "project_id": str(uuid.uuid4()),
        "plan_digest": "d" * 64, "server_name": BOARD_SERVER, "device_id": DEVICE_ID, "device_kind": "mcu",
    }
    program = HardwareActionV2ApprovalPayload.model_validate(
        {**base, "action_class": "program", "image_sha256": "e" * 64, "image_remote_path": worker_image_path(BOARD_CHECKOUT, "e" * 64)}
    )
    assert program.power_sequence is None
    with pytest.raises(ValidationError, match="program actions pin an image"):
        HardwareActionV2ApprovalPayload.model_validate({**base, "action_class": "program"})
    with pytest.raises(ValidationError, match="only program actions pin an image"):
        HardwareActionV2ApprovalPayload.model_validate(
            {**base, "action_class": "hil_test", "image_sha256": "e" * 64, "image_remote_path": "/x/y.bin"}
        )
    with pytest.raises(ValidationError, match="power actions pin a power_sequence"):
        HardwareActionV2ApprovalPayload.model_validate({**base, "action_class": "power"})
    with pytest.raises(ValidationError, match="only power actions pin"):
        HardwareActionV2ApprovalPayload.model_validate(
            {**base, "action_class": "hil_test", "power_sequence": "reset"}
        )
    power = HardwareActionV2ApprovalPayload.model_validate(
        {**base, "action_class": "power", "power_sequence": "off_on"}
    )
    assert power.image_remote_path is None


# ---------------------------------------------------------------------------
# kind registration (never auto-approved, transaction-only, human-readable)
# ---------------------------------------------------------------------------


def test_kind_is_registered_transaction_only_and_never_auto_approved(tmp_path):
    assert HARDWARE_ACTION_V2_APPROVAL_KIND in VALID_APPROVAL_KINDS
    assert HARDWARE_ACTION_V2_APPROVAL_KIND in TRANSACTION_ONLY_APPROVAL_KINDS
    db = Database(str(tmp_path / "kind.db"))
    approval_id = db.insert_approval(kind=HARDWARE_ACTION_V2_APPROVAL_KIND, payload={}, requester_actor_id=None)
    approval = db.get_approval(approval_id)
    assert asyncio.run(maybe_auto_approve(db, approval, source="web", rules=[{"kind": "any"}])) is None
    assert db.get_approval(approval_id).status == "pending"
    with pytest.raises(ValueError, match="must use the Product v2 decision"):
        asyncio.run(approval_module.approve(db, approval_id))
    assert db.get_approval(approval_id).status == "pending"
    presented = describe_approval(
        HARDWARE_ACTION_V2_APPROVAL_KIND,
        {"action_class": "program", "server_name": BOARD_SERVER, "device_id": DEVICE_ID, "image_sha256": "f" * 64},
    )
    assert presented["title"] == "硬體實體動作"
    assert "燒錄" in presented["summary"] and DEVICE_ID in presented["summary"]
    db.close()


# ---------------------------------------------------------------------------
# end to end through the API
# ---------------------------------------------------------------------------


class _RecordingPool:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.fail = fail

    async def put_file(self, server, local_path, remote_path):
        self.calls.append((server.name, local_path, remote_path))
        if self.fail:
            raise RuntimeError("sftp down")

    async def close_all(self) -> None:  # lifespan shutdown hook
        return None


def _publish_board_server(database: Database, *, project_name: str, git_commit: str, present: str = "present") -> dict:
    """A second server that declares the board, with a fresh present observation and an instance."""

    server = {
        "name": BOARD_SERVER,
        "enabled": True,
        "execution_backend": "ssh",
        "host": "192.0.2.118",
        "port": 22,
        "user": "worker",
        "key": "/dispatch-test/nonexistent-p3-key",
        "project_roots": ["/srv/projects"],
        "dataset_roots": ["/srv/datasets"],
        "tags": ["gpu"],
        "devices": [{"id": DEVICE_ID, "kind": "mcu", "presence": "path:/dev/ttyUSB0", "model": "esp32"}],
    }
    before: dict[str, list[dict[str, object]]] = {"servers": []}
    after = {"servers": [server]}
    contract = env_tests.build_server_config_contract(
        operation="add", server_name=BOARD_SERVER, yaml_before=before, yaml_after=after, server_payload=server,
    )
    approval_id = database.insert_pinned_approval(
        kind="server_add", contract_version=env_tests.SERVER_CONFIG_CONTRACT_VERSION, payload=contract,
    )
    outcome = env_tests.publish_approved_server_mutation(
        database, approval_id=approval_id, operation="add", server_name=BOARD_SERVER,
        server_payload=server, yaml_before=before, yaml_after=after,
        decision_actor_id="human-reviewer", write_yaml=lambda: None,
    )
    assert outcome.state == "activated"
    revision = database.get_active_server_config_revision(BOARD_SERVER)
    assert revision is not None
    revision = database.record_server_attempt_backend_preflight(
        server_name=BOARD_SERVER, revision_id=revision["id"], status="eligible",
        contract_version=ATTEMPT_FILESYSTEM_PREFLIGHT_CONTRACT_VERSION, filesystem_type="ext2/ext3/ext4",
    )
    _observe_board(database, present)
    instance_id = database.insert_project_instance(
        project_name=project_name, server=BOARD_SERVER, path=BOARD_CHECKOUT,
        git_branch="main", git_commit=git_commit, dirty=False,
    )
    database.update_instance_reconcile(
        instance_id, state="available", git_branch="main", git_commit=git_commit, dirty=False, touch_last_seen=True,
    )
    return {"revision": revision, "instance_id": instance_id}


def _observe_board(database: Database, present: str | None) -> None:
    database.insert_server_observation(
        server_name=BOARD_SERVER, online=True, probe_ok=True, gpu_count=2,
        gpu_mem_used_mb=1024, gpu_mem_total_mb=24576,
        mem_total_bytes=64 * 1024**3, mem_available_bytes=32 * 1024**3, disk_avail_bytes=512 * 1024**3,
        devices_json=None if present is None else '{"%s": "%s"}' % (DEVICE_ID, present),
    )


def _seed_physical_context(client, main_module, *, present: str = "present") -> dict:
    """A project with an approved build plan + registered image, a board server, and a program template."""

    seed = _seed_approved_build_plan(client, main_module)
    database = seed["db"]
    config = main_module.app_state.config
    content = b"\x7fELF-firmware-" + uuid.uuid4().bytes
    _write_result(config.local_home_dir, seed["job_id"], "build/firmware.bin", content)
    job = database.get_job(seed["job_id"])
    register_build_images(job, db=database, config=config, audit_path=str(Path(config.local_home_dir) / "audit.jsonl"))
    sha256 = hashlib.sha256(content).hexdigest()
    assert database.get_hardware_image_by_sha256(sha256) is not None
    board = _publish_board_server(
        database, project_name=seed["project_name"], git_commit=seed["version"]["git_commit"], present=present,
    )
    main_module.app_state.server_configs[BOARD_SERVER] = ServerConfig(
        name=BOARD_SERVER, host="192.0.2.118", user="worker", key="/dispatch-test/nonexistent-p3-key", port=22, enabled=True,
    )
    template = _approve_template(
        database, seed["project_id"], seed["environment"]["environment_revision_id"], _program_template(),
    )
    return {**seed, "image_sha256": sha256, "board": board, "program": template}


def _action_request(seed: dict, run_profile_id: str, **pins) -> dict:
    body = {
        "project_version_id": seed["version"]["id"],
        "template_selection": {"kind": "run_profile_revision", "run_profile_id": run_profile_id},
        "parameter_overrides": {},
        "dataset_selection": {"kind": "none"},
        "target_selection": {"kind": "server_config_revision", "server_config_revision_id": seed["board"]["revision"]["id"]},
        "device_id": DEVICE_ID,
    }
    body.update(pins)
    return body


def _reason(response) -> str:
    return response.json()["error"]["details"]["reason"]


@pytest.mark.untraced
def test_program_action_round_trips_and_pushes_the_verified_image(api_client):
    client, main_module = api_client
    seed = _seed_physical_context(client, main_module)
    database = seed["db"]
    pool = _RecordingPool()
    main_module.app_state.ssh_pool = pool
    _session_for(client, main_module, OPERATOR_ID)
    body = _action_request(seed, seed["program"]["run_profile_id"], image_sha256=seed["image_sha256"])
    expected_path = worker_image_path(BOARD_CHECKOUT, seed["image_sha256"])

    preview = client.post(f"/api/v2/projects/{seed['project_id']}/hardware-action-previews", json=body)
    assert preview.status_code == 200, preview.json()
    assert preview.json()["approval_kind"] == HARDWARE_ACTION_V2_APPROVAL_KIND
    assert preview.json()["hardware"] == {
        "action_class": "program", "server_name": BOARD_SERVER, "device_id": DEVICE_ID,
        "device_kind": "mcu", "image_sha256": seed["image_sha256"], "power_sequence": None,
    }
    assert preview.json()["plan"]["target"]["server_name"] == BOARD_SERVER

    submitted = client.post(
        f"/api/v2/projects/{seed['project_id']}/hardware-action-requests",
        headers={"Idempotency-Key": "p3-program-submit"},
        json={**body, "expected_plan_digest": preview.json()["plan_digest"]},
    )
    assert submitted.status_code == 202, submitted.json()
    approval_id = submitted.json()["approval_id"]
    approval = database.get_approval(approval_id)
    assert approval.kind == HARDWARE_ACTION_V2_APPROVAL_KIND and approval.status == "pending"
    assert approval.payload["image_remote_path"] == expected_path
    assert approval.payload["device_id"] == DEVICE_ID
    assert pool.calls == []  # nothing pushed before a human decides

    #: the card shows the physical pins
    _session_for(client, main_module, REVIEWER_ID)
    detail = client.get(f"/api/v2/approvals/{approval_id}")
    assert detail.status_code == 200, detail.json()
    review = detail.json().get("review") or detail.json().get("review_payload") or {}
    assert review.get("device_id") == DEVICE_ID and review.get("image_sha256") == seed["image_sha256"]

    #: the requester cannot decide their own physical action
    _session_for(client, main_module, OPERATOR_ID)
    own = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "p3-self"},
        json={"decision": "approve", "note": "me"},
    )
    assert own.status_code == 403, own.json()

    _session_for(client, main_module, REVIEWER_ID)
    approved = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "p3-program-approve"},
        json={"decision": "approve", "note": "flash it"},
    )
    assert approved.status_code == 202, approved.json()
    assert approved.json()["status"] == "approved"
    job_id = approved.json()["job_id"]
    assert pool.calls == [(BOARD_SERVER, str(image_store_path(main_module.app_state.config.local_home_dir, seed["image_sha256"])), expected_path)]
    job = database.get_job(job_id)
    assert job.pin_server == BOARD_SERVER and job.status == "queued"
    assert f"exec esptool write_flash {expected_path}" in job.command
    assert f"test -f {expected_path}" in job.command
    assert seed["image_sha256"] in job.command
    assert database.get_approval(approval_id).status == "approved"

    #: replay is idempotent and pushes nothing twice
    again = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "p3-program-approve"},
        json={"decision": "approve", "note": "flash it"},
    )
    assert again.status_code == 202 and again.json()["replayed"] is True and again.json()["job_id"] == job_id
    assert len(pool.calls) == 1


@pytest.mark.untraced
def test_physical_pins_are_refused_with_named_reasons(api_client):
    client, main_module = api_client
    seed = _seed_physical_context(client, main_module)
    _session_for(client, main_module, OPERATOR_ID)
    url = f"/api/v2/projects/{seed['project_id']}/hardware-action-previews"
    program = seed["program"]["run_profile_id"]

    #: a compute template never runs through the physical path
    r = client.post(url, json=_action_request(seed, seed["template"]["run_profile_id"], image_sha256=seed["image_sha256"]))
    assert r.status_code == 409 and _reason(r) == "hardware_compute_template", r.json()
    #: program needs an image, and only a registered one
    r = client.post(url, json=_action_request(seed, program))
    assert r.status_code == 409 and _reason(r) == "hardware_image_required"
    r = client.post(url, json=_action_request(seed, program, image_sha256="0" * 64))
    assert r.status_code == 409 and _reason(r) == "hardware_image_unavailable"
    #: a power sequence is only for power actions
    r = client.post(url, json=_action_request(seed, program, image_sha256=seed["image_sha256"], power_sequence="reset"))
    assert r.status_code == 409 and _reason(r) == "hardware_power_sequence_not_applicable"
    #: the device must be declared on that server, with the template's kind
    r = client.post(url, json=_action_request(seed, program, image_sha256=seed["image_sha256"], device_id="nope-1"))
    assert r.status_code == 409 and _reason(r) == "hardware_device_not_declared"
    #: the same template still cannot go through a run or an experiment
    run = client.post(
        f"/api/v2/projects/{seed['project_id']}/run-previews",
        json={k: v for k, v in _action_request(seed, program).items() if k != "device_id"},
    )
    assert run.status_code == 409 and _reason(run) == "hardware_action_required"


@pytest.mark.untraced
def test_absent_or_unknown_device_observation_blocks_request_and_decision(api_client):
    client, main_module = api_client
    seed = _seed_physical_context(client, main_module, present="absent")
    database = seed["db"]
    _session_for(client, main_module, OPERATOR_ID)
    url = f"/api/v2/projects/{seed['project_id']}/hardware-action-previews"
    body = _action_request(seed, seed["program"]["run_profile_id"], image_sha256=seed["image_sha256"])
    r = client.post(url, json=body)
    assert r.status_code == 409 and _reason(r) == "target_device_absent", r.json()
    _observe_board(database, None)
    r = client.post(url, json=body)
    assert r.status_code == 409 and _reason(r) == "target_device_observation_unknown"

    #: present at request time, unplugged before the decision: stale-rejected, nothing pushed
    _observe_board(database, "present")
    preview = client.post(url, json=body)
    assert preview.status_code == 200, preview.json()
    submitted = client.post(
        f"/api/v2/projects/{seed['project_id']}/hardware-action-requests",
        headers={"Idempotency-Key": "p3-absent-submit"},
        json={**body, "expected_plan_digest": preview.json()["plan_digest"]},
    )
    assert submitted.status_code == 202, submitted.json()
    approval_id = submitted.json()["approval_id"]
    _observe_board(database, "absent")
    pool = _RecordingPool()
    main_module.app_state.ssh_pool = pool
    _session_for(client, main_module, REVIEWER_ID)
    decided = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "p3-absent-approve"},
        json={"decision": "approve", "note": "go"},
    )
    assert decided.status_code == 202, decided.json()
    assert decided.json()["status"] == "rejected"
    assert database.get_approval(approval_id).status == "rejected"
    assert database.get_approval(approval_id).note == "execution_plan_stale"
    assert pool.calls == [(BOARD_SERVER, str(image_store_path(main_module.app_state.config.local_home_dir, seed["image_sha256"])), worker_image_path(BOARD_CHECKOUT, seed["image_sha256"]))]
    with database.cursor() as cur:
        assert cur.execute("SELECT COUNT(*) FROM jobs WHERE execution_approval_id = ?", (approval_id,)).fetchone()[0] == 0


@pytest.mark.untraced
def test_push_failure_or_tampered_store_keeps_the_card_pending(api_client):
    client, main_module = api_client
    seed = _seed_physical_context(client, main_module)
    database = seed["db"]
    _session_for(client, main_module, OPERATOR_ID)
    body = _action_request(seed, seed["program"]["run_profile_id"], image_sha256=seed["image_sha256"])
    preview = client.post(f"/api/v2/projects/{seed['project_id']}/hardware-action-previews", json=body)
    assert preview.status_code == 200, preview.json()
    submitted = client.post(
        f"/api/v2/projects/{seed['project_id']}/hardware-action-requests",
        headers={"Idempotency-Key": "p3-push-submit"},
        json={**body, "expected_plan_digest": preview.json()["plan_digest"]},
    )
    approval_id = submitted.json()["approval_id"]
    _session_for(client, main_module, REVIEWER_ID)

    main_module.app_state.ssh_pool = _RecordingPool(fail=True)
    failed = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "p3-push-approve-1"},
        json={"decision": "approve", "note": "go"},
    )
    assert failed.status_code == 409 and _reason(failed) == "hardware_image_push_failed", failed.json()
    assert database.get_approval(approval_id).status == "pending"

    stored = image_store_path(main_module.app_state.config.local_home_dir, seed["image_sha256"])
    stored.chmod(0o644)
    stored.write_bytes(b"tampered")
    main_module.app_state.ssh_pool = _RecordingPool()
    tampered = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "p3-push-approve-2"},
        json={"decision": "approve", "note": "go"},
    )
    assert tampered.status_code == 409 and _reason(tampered) == "hardware_image_digest_mismatch"
    assert database.get_approval(approval_id).status == "pending"
    assert main_module.app_state.ssh_pool.calls == []

    #: a reject needs no push and closes the card
    rejected = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": "p3-push-reject"},
        json={"decision": "reject", "note": "not today"},
    )
    assert rejected.status_code == 202 and rejected.json()["status"] == "rejected"
    assert main_module.app_state.ssh_pool.calls == []
