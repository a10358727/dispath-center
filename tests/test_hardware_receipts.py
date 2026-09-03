"""DG-HARDWARE-EXECUTION v1 P3b: receipts, known-good marks, physical tools, power sequences.

Pins (H-2/H-4/H-6):
- `hardware-receipt-v1` parses purely and totally (closed keys, bounds, verify enum);
- the job-finish hook stores one receipt row per physical action, cross-checked
  against the approved device/image, never touching the job's terminal state;
- an image becomes known-good only by a human: a `hil_test` decider's flag applied
  by a verified receipt, or a platform admin's direct mark (INV-APPROVAL-1 exception);
- an environment declares `physical_tools` from a closed vocabulary and a compute or
  build template naming one is refused at card creation (400);
- a `power` template takes exactly one `power_sequence` token mapped from the
  device's declared `power_control`, and a `power` action needs such a device.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.db import Database
from app.hardware_actions import POWER_SEQUENCE_LITERALS, power_sequence_literal
from app.hardware_receipts import (
    MAX_RECEIPT_BYTES,
    RECEIPT_KEYS,
    collect_hardware_receipt,
    cross_check_receipt,
    parse_hardware_receipt_v1,
)
from app.jobfinish import handle_job_finished
from app.project_bootstrap import (
    PHYSICAL_TOOLS,
    EnvironmentRevisionInput,
    RunTemplateSpecInput,
    physical_tool_in_compute_template,
)
from app.run_templates import compile_structured_argv
from tests.test_hardware_actions import (
    DEVICE_ID,
    RELAY_ID,
    _action_request,
    _program_template,
    _reason,
    _RecordingPool,
    _seed_physical_context,
)
from tests.test_hardware_images import _approve_template, _build_template
from tests.test_jobfinish import RecordingLocalRun, make_recording_send_mail, make_server_cfg
from tests.test_run_templates_v2 import (
    OPERATOR_ID,
    REVIEWER_ID,
    _environment_input,
    _request_template,
    _seed_project,
    _session_for,
)


# ---------------------------------------------------------------------------
# receipt contract (pure)
# ---------------------------------------------------------------------------


def _receipt(**overrides) -> dict:
    body = {
        "device_id": DEVICE_ID,
        "device_serial_observed": "ESP32-ABC123",
        "image_sha256": "a" * 64,
        "tool": "esptool",
        "tool_version": "4.7",
        "verify": "verified",
        "exit_code": 0,
    }
    body.update(overrides)
    return body


def _raw(body: dict) -> bytes:
    return json.dumps(body).encode("utf-8")


def test_receipt_parser_is_closed_and_total():
    parsed = parse_hardware_receipt_v1(_raw(_receipt()))
    assert parsed.status == "collected" and parsed.reason is None
    assert tuple(parsed.fields) == RECEIPT_KEYS
    assert parse_hardware_receipt_v1(b"x" * (MAX_RECEIPT_BYTES + 1)).status == "oversize"
    assert parse_hardware_receipt_v1(b"\xff\xfe").reason == "invalid_utf8"
    assert parse_hardware_receipt_v1(b"{").reason == "invalid_json"
    assert parse_hardware_receipt_v1(b"[]").reason == "top_level_not_object"
    assert parse_hardware_receipt_v1(_raw({**_receipt(), "extra": 1})).reason == "keys_not_closed"
    body = _receipt()
    del body["tool"]
    assert parse_hardware_receipt_v1(_raw(body)).reason == "keys_not_closed"
    assert parse_hardware_receipt_v1(_raw(_receipt(device_id="bad id"))).reason == "invalid_device_id"
    assert parse_hardware_receipt_v1(_raw(_receipt(image_sha256="zz"))).reason == "invalid_image_sha256"
    assert parse_hardware_receipt_v1(_raw(_receipt(verify="maybe"))).reason == "invalid_verify"
    assert parse_hardware_receipt_v1(_raw(_receipt(exit_code=True))).reason == "invalid_exit_code"
    assert parse_hardware_receipt_v1(_raw(_receipt(exit_code="0"))).reason == "invalid_exit_code"
    assert parse_hardware_receipt_v1(_raw(_receipt(tool=""))).reason == "invalid_tool"
    assert parse_hardware_receipt_v1(_raw(_receipt(tool="a\nb"))).reason == "invalid_tool"
    #: optional fields may be null; a power receipt has no image
    nulls = parse_hardware_receipt_v1(_raw(_receipt(device_serial_observed=None, image_sha256=None, tool_version=None)))
    assert nulls.status == "collected" and nulls.fields["image_sha256"] is None


def test_receipt_cross_check_pins_device_and_image():
    fields = _receipt()
    assert cross_check_receipt(fields, device_id=DEVICE_ID, image_sha256="a" * 64) is None
    assert cross_check_receipt(fields, device_id="other", image_sha256="a" * 64) == "device_id_mismatch"
    assert cross_check_receipt(fields, device_id=DEVICE_ID, image_sha256="b" * 64) == "image_sha256_mismatch"
    assert cross_check_receipt(fields, device_id=DEVICE_ID, image_sha256=None) is None


# ---------------------------------------------------------------------------
# H-6 physical tools + power sequence contract (pure)
# ---------------------------------------------------------------------------


def test_environment_physical_tools_are_closed_and_digest_stable():
    plain = _environment_input()
    assert plain.physical_tools == [] and "physical_tools" not in plain.model_dump(mode="json")
    declared = EnvironmentRevisionInput.model_validate({**plain.model_dump(mode="json"), "physical_tools": ["openocd", "esptool", "openocd"]})
    assert declared.physical_tools == ["esptool", "openocd"]
    assert declared.model_dump(mode="json")["physical_tools"] == ["esptool", "openocd"]
    with pytest.raises(ValidationError, match="closed vocabulary"):
        EnvironmentRevisionInput.model_validate({**plain.model_dump(mode="json"), "physical_tools": ["rm"]})
    assert "uhubctl" in PHYSICAL_TOOLS


def test_compute_template_naming_a_physical_tool_is_detected_by_basename():
    build = _build_template(argv_template=[{"kind": "literal", "value": "/usr/bin/esptool"}, {"kind": "literal", "value": "flash"}])
    assert physical_tool_in_compute_template(build, ["esptool"]) == "esptool"
    assert physical_tool_in_compute_template(build, ["openocd"]) is None
    assert physical_tool_in_compute_template(build, []) is None
    #: a physical template may of course name its tool
    assert physical_tool_in_compute_template(_program_template(), ["esptool"]) is None


def _power_template(**overrides) -> RunTemplateSpecInput:
    body = {
        "name": "power-cycle",
        "action_class": "power",
        "argv_template": [
            {"kind": "literal", "value": "uhubctl"},
            {"kind": "literal", "value": "-a"},
            {"kind": "power_sequence"},
        ],
        "parameter_schema": [],
        "resource_requirements": {"required_tags": ["gpu"], "required_devices": [{"kind": "power"}]},
        "output_declarations": [],
    }
    body.update(overrides)
    return RunTemplateSpecInput.model_validate(body)


def test_power_sequence_token_is_bound_to_power_templates_and_closed_literals():
    template = _power_template()
    with pytest.raises(ValidationError, match="exactly one power_sequence"):
        _power_template(argv_template=[{"kind": "literal", "value": "uhubctl"}])
    with pytest.raises(ValidationError, match="requires action_class power"):
        _program_template(argv_template=[{"kind": "literal", "value": "x"}, {"kind": "image"}, {"kind": "power_sequence"}])
    with pytest.raises(ValueError, match="power literal"):
        compile_structured_argv(template, {})
    assert compile_structured_argv(template, {}, power_literal="cycle").argv == ("uhubctl", "-a", "cycle")
    assert power_sequence_literal("usb_relay", "off_on") == "cycle"
    assert power_sequence_literal("pdu_http", "reset") == "reboot"
    assert set(POWER_SEQUENCE_LITERALS) == {"usb_relay", "pdu_http"}
    with pytest.raises(ValueError, match="hardware_device_no_power_control"):
        power_sequence_literal(None, "off_on")
    with pytest.raises(ValueError, match="hardware_power_sequence_required"):
        power_sequence_literal("usb_relay", None)


# ---------------------------------------------------------------------------
# H-6 rule at card creation
# ---------------------------------------------------------------------------


def _create_environment_with_tools(database: Database, project_id: str, tools: list[str]) -> dict:
    from dispatch_center.infrastructure.db import SQLiteUnitOfWork

    environment = EnvironmentRevisionInput.model_validate(
        {**_environment_input().model_dump(mode="json"), "physical_tools": tools}
    )
    with SQLiteUnitOfWork(database) as unit_of_work:
        approval_id = unit_of_work.run(
            lambda cursor: database.create_environment_change_approval_in_transaction(
                cursor,
                project_id=project_id,
                operation="create",
                expected_revision=0,
                expected_head_revision_id=None,
                environment_id=None,
                environment=environment,
                requester_actor_id=OPERATOR_ID,
            )
        )
    return database.apply_environment_change_decision(
        approval_id=approval_id,
        decision_actor_id=REVIEWER_ID,
        decision_mechanism="session",
    )


def test_compute_template_with_declared_physical_tool_is_refused_at_request(tmp_path):
    database = Database(str(tmp_path / "h6.db"))
    project_id = _seed_project(database)
    #: a revision that declares esptool physical (revisions are immutable: create it so)
    environment = _create_environment_with_tools(database, project_id, ["esptool"])
    offending = _build_template(argv_template=[{"kind": "literal", "value": "esptool"}, {"kind": "literal", "value": "image_info"}])
    with pytest.raises(ValueError, match="physical_tool_in_compute_template"):
        _request_template(
            database, project_id=project_id,
            environment_revision_id=environment["environment_revision_id"], template=offending,
        )
    #: the same tool inside a program template is fine, and the declaration round-trips
    _request_template(
        database, project_id=project_id,
        environment_revision_id=environment["environment_revision_id"], template=_program_template(),
    )
    with database.cursor() as cur:
        row = cur.execute(
            "SELECT physical_tools_json FROM environment_revisions WHERE id = ?",
            (environment["environment_revision_id"],),
        ).fetchone()
    assert json.loads(row["physical_tools_json"]) == ["esptool"]
    database.close()


# ---------------------------------------------------------------------------
# job-finish collection + known-good (end to end)
# ---------------------------------------------------------------------------


def _write_receipt(local_home_dir: str, job_id: int, body: dict | bytes) -> None:
    path = Path(local_home_dir) / "results" / str(job_id) / "hardware_receipt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body if isinstance(body, bytes) else _raw(body))


def _approve_action(client, main_module, seed, body, *, key: str, mark_known_good: bool | None = None) -> tuple[int, int]:
    _session_for(client, main_module, OPERATOR_ID)
    preview = client.post(f"/api/v2/projects/{seed['project_id']}/hardware-action-previews", json=body)
    assert preview.status_code == 200, preview.json()
    submitted = client.post(
        f"/api/v2/projects/{seed['project_id']}/hardware-action-requests",
        headers={"Idempotency-Key": f"{key}-submit"},
        json={**body, "expected_plan_digest": preview.json()["plan_digest"]},
    )
    assert submitted.status_code == 202, submitted.json()
    approval_id = submitted.json()["approval_id"]
    _session_for(client, main_module, REVIEWER_ID)
    decision = {"decision": "approve", "note": "go"}
    if mark_known_good is not None:
        decision["mark_known_good"] = mark_known_good
    approved = client.post(
        f"/api/v2/approvals/{approval_id}/decisions",
        headers={"Idempotency-Key": f"{key}-approve"},
        json=decision,
    )
    assert approved.status_code == 202, approved.json()
    assert approved.json()["status"] == "approved"
    return approval_id, approved.json()["job_id"]


def _finish(main_module, job, audit_path: str) -> None:
    asyncio.run(
        handle_job_finished(
            job,
            server_cfg=make_server_cfg(),
            local_run=RecordingLocalRun([]),
            config=main_module.app_state.config,
            audit_path=audit_path,
            db=main_module.app_state.db,
            send_mail=make_recording_send_mail([]),
        )
    )


def _audit_actions(audit_path: str) -> list[str]:
    if not os.path.exists(audit_path):
        return []
    with open(audit_path, encoding="utf-8") as fh:
        return [json.loads(line)["action"] for line in fh if line.strip()]


@pytest.mark.untraced
def test_program_receipt_is_collected_cross_checked_and_never_changes_job_status(api_client):
    client, main_module = api_client
    seed = _seed_physical_context(client, main_module)
    database = seed["db"]
    main_module.app_state.ssh_pool = _RecordingPool()
    body = _action_request(seed, seed["program"]["run_profile_id"], image_sha256=seed["image_sha256"])
    approval_id, job_id = _approve_action(client, main_module, seed, body, key="p3b-program")
    audit_path = str(Path(main_module.app_state.config.local_home_dir) / "p3b-audit.jsonl")
    job = database.get_job(job_id)

    #: verified receipt naming the approved device and image
    _write_receipt(main_module.app_state.config.local_home_dir, job_id, _receipt(image_sha256=seed["image_sha256"]))
    collect_hardware_receipt(job, db=database, config=main_module.app_state.config, audit_path=audit_path)
    receipt = database.get_hardware_receipt(job_id)
    assert receipt["status"] == "collected" and receipt["approval_id"] == approval_id
    assert json.loads(receipt["receipt_json"])["verify"] == "verified"
    assert receipt["action_class"] == "program"
    assert database.get_job(job_id).status == job.status
    assert "hardware_receipt_collected" in _audit_actions(audit_path)
    #: no intent was recorded on a program card: nothing becomes known-good by itself
    image = database.get_hardware_image_by_sha256(seed["image_sha256"])
    assert image["known_good_marked_at"] is None

    #: a receipt naming another image is invalid (pins mismatch), replaced in place
    _write_receipt(main_module.app_state.config.local_home_dir, job_id, _receipt(image_sha256="c" * 64))
    collect_hardware_receipt(job, db=database, config=main_module.app_state.config, audit_path=audit_path)
    receipt = database.get_hardware_receipt(job_id)
    assert receipt["status"] == "invalid" and receipt["reason"] == "image_sha256_mismatch"
    assert receipt["receipt_json"] is None

    #: missing = unknown; oversize = oversize; garbage = invalid — none raise
    os.remove(Path(main_module.app_state.config.local_home_dir) / "results" / str(job_id) / "hardware_receipt.json")
    collect_hardware_receipt(job, db=database, config=main_module.app_state.config, audit_path=audit_path)
    assert database.get_hardware_receipt(job_id)["status"] == "missing"
    _write_receipt(main_module.app_state.config.local_home_dir, job_id, b"{" * (MAX_RECEIPT_BYTES + 1))
    collect_hardware_receipt(job, db=database, config=main_module.app_state.config, audit_path=audit_path)
    assert database.get_hardware_receipt(job_id)["status"] == "oversize"

    #: the full finish hook runs it too, and a compute job is ignored entirely
    _write_receipt(main_module.app_state.config.local_home_dir, job_id, _receipt(image_sha256=seed["image_sha256"]))
    database.update_job(job_id, status="done", exit_code=0)
    _finish(main_module, database.get_job(job_id), audit_path)
    assert database.get_hardware_receipt(job_id)["status"] == "collected"
    build_job = database.get_job(seed["job_id"])
    _write_receipt(main_module.app_state.config.local_home_dir, seed["job_id"], _receipt())
    collect_hardware_receipt(build_job, db=database, config=main_module.app_state.config, audit_path=audit_path)
    assert database.get_hardware_receipt(seed["job_id"]) is None

    #: the project's receipts are listable
    _session_for(client, main_module, OPERATOR_ID)
    listed = client.get(f"/api/v2/projects/{seed['project_id']}/hardware-receipts")
    assert listed.status_code == 200, listed.json()
    assert [item["job_id"] for item in listed.json()["items"]] == [job_id]
    assert listed.json()["items"][0]["execution_plan_id"]


@pytest.mark.untraced
def test_hil_test_decider_flag_marks_known_good_only_through_a_verified_receipt(api_client):
    client, main_module = api_client
    seed = _seed_physical_context(client, main_module)
    database = seed["db"]
    main_module.app_state.ssh_pool = _RecordingPool()
    hil = _approve_template(
        database, seed["project_id"], seed["environment"]["environment_revision_id"],
        _program_template(name="hil", action_class="hil_test", argv_template=[{"kind": "literal", "value": "pytest"}, {"kind": "literal", "value": "tests/hil"}]),
    )
    body = _action_request(seed, hil["run_profile_id"])
    #: the flag is refused on a program card
    _session_for(client, main_module, OPERATOR_ID)
    program_body = _action_request(seed, seed["program"]["run_profile_id"], image_sha256=seed["image_sha256"])
    preview = client.post(f"/api/v2/projects/{seed['project_id']}/hardware-action-previews", json=program_body)
    submitted = client.post(
        f"/api/v2/projects/{seed['project_id']}/hardware-action-requests",
        headers={"Idempotency-Key": "p3b-flag-program-submit"},
        json={**program_body, "expected_plan_digest": preview.json()["plan_digest"]},
    )
    _session_for(client, main_module, REVIEWER_ID)
    refused = client.post(
        f"/api/v2/approvals/{submitted.json()['approval_id']}/decisions",
        headers={"Idempotency-Key": "p3b-flag-program-approve"},
        json={"decision": "approve", "note": "x", "mark_known_good": True},
    )
    assert refused.status_code == 409 and _reason(refused) == "hardware_known_good_requires_hil_test", refused.json()
    assert database.get_approval(submitted.json()["approval_id"]).status == "pending"

    approval_id, job_id = _approve_action(client, main_module, seed, body, key="p3b-hil", mark_known_good=True)
    assert main_module.app_state.ssh_pool.calls == []  # a hil_test pushes no image
    assert database.get_known_good_intent(approval_id)["actor_id"] == REVIEWER_ID
    audit_path = str(Path(main_module.app_state.config.local_home_dir) / "p3b-audit.jsonl")
    job = database.get_job(job_id)
    home = main_module.app_state.config.local_home_dir

    #: unverified receipt: intent stays, image not marked
    _write_receipt(home, job_id, _receipt(image_sha256=seed["image_sha256"], verify="unverified", tool="pytest"))
    collect_hardware_receipt(job, db=database, config=main_module.app_state.config, audit_path=audit_path)
    assert database.get_hardware_image_by_sha256(seed["image_sha256"])["known_good_marked_at"] is None
    #: verified receipt naming a registered image: marked once, with provenance
    _write_receipt(home, job_id, _receipt(image_sha256=seed["image_sha256"], verify="verified", tool="pytest"))
    collect_hardware_receipt(job, db=database, config=main_module.app_state.config, audit_path=audit_path)
    image = database.get_hardware_image_by_sha256(seed["image_sha256"])
    assert image["known_good_source"] == "decision"
    assert image["known_good_marked_by_approval_id"] == approval_id
    assert image["known_good_marked_by_actor_id"] == REVIEWER_ID
    assert image["known_good_marked_at"] is not None
    assert _audit_actions(audit_path).count("hardware_image_known_good_marked") == 1
    #: re-collecting does not re-mark
    collect_hardware_receipt(job, db=database, config=main_module.app_state.config, audit_path=audit_path)
    assert _audit_actions(audit_path).count("hardware_image_known_good_marked") == 1
    listed = client.get(f"/api/v2/projects/{seed['project_id']}/hardware-images")
    assert listed.json()["items"][0]["known_good_source"] == "decision"


@pytest.mark.untraced
def test_platform_admin_marks_known_good_directly_and_idempotently(api_client):
    client, main_module = api_client
    seed = _seed_physical_context(client, main_module)
    database = seed["db"]
    image = database.get_hardware_image_by_sha256(seed["image_sha256"])
    url = f"/api/v2/projects/{seed['project_id']}/hardware-images/{image['id']}/known-good"
    #: a project operator is not a platform admin
    _session_for(client, main_module, OPERATOR_ID)
    assert client.post(url).status_code == 403
    from tests.test_identity_workspace_v2 import _create_human

    admin = _create_human(database, platform_admin=True)
    admin_id = getattr(admin, "actor_id", None) or getattr(admin, "id", None) or admin
    _session_for(client, main_module, str(admin_id))
    marked = client.post(url)
    assert marked.status_code == 200, marked.json()
    assert marked.json()["marked_now"] is True
    assert marked.json()["image"]["known_good_source"] == "direct"
    assert marked.json()["image"]["known_good_marked_by_approval_id"] is None
    again = client.post(url)
    assert again.status_code == 200 and again.json()["marked_now"] is False
    assert again.json()["image"]["known_good_marked_at"] == marked.json()["image"]["known_good_marked_at"]
    assert client.post(f"/api/v2/projects/{uuid.uuid4()}/hardware-images/{image['id']}/known-good").status_code == 404
    assert client.post(f"/api/v2/projects/{seed['project_id']}/hardware-images/{uuid.uuid4()}/known-good").status_code == 404


@pytest.mark.untraced
def test_power_action_needs_a_power_device_and_compiles_the_closed_literal(api_client):
    client, main_module = api_client
    seed = _seed_physical_context(client, main_module)
    database = seed["db"]
    main_module.app_state.ssh_pool = _RecordingPool()
    power = _approve_template(
        database, seed["project_id"], seed["environment"]["environment_revision_id"], _power_template(),
    )
    _session_for(client, main_module, OPERATOR_ID)
    url = f"/api/v2/projects/{seed['project_id']}/hardware-action-previews"
    #: the board is an mcu, not a power device
    r = client.post(url, json=_action_request(seed, power["run_profile_id"], power_sequence="off_on"))
    assert r.status_code == 409 and _reason(r) == "hardware_device_kind_mismatch", r.json()
    #: without a sequence the request is incomplete
    r = client.post(url, json=_action_request(seed, power["run_profile_id"]))
    assert r.status_code == 409 and _reason(r) == "hardware_power_sequence_required", r.json()
    #: the relay declared next to the board carries `power_control: usb_relay`
    body = _action_request(seed, power["run_profile_id"], device_id=RELAY_ID, power_sequence="off_on")
    preview = client.post(url, json=body)
    assert preview.status_code == 200, preview.json()
    assert preview.json()["hardware"]["power_sequence"] == "off_on"
    assert preview.json()["hardware"]["device_kind"] == "power"
    approval_id, job_id = _approve_action(client, main_module, seed, body, key="p3b-power")
    assert main_module.app_state.ssh_pool.calls == []  # power pushes no image
    job = database.get_job(job_id)
    assert "exec uhubctl -a cycle" in job.command
    assert "sha256sum" not in job.command
    approval = database.get_approval(approval_id)
    assert approval.payload["action_class"] == "power" and approval.payload["power_sequence"] == "off_on"
    assert approval.payload["image_sha256"] is None
    #: its receipt has no image and is still cross-checked on the device
    audit_path = str(Path(main_module.app_state.config.local_home_dir) / "p3b-audit.jsonl")
    _write_receipt(main_module.app_state.config.local_home_dir, job_id, _receipt(device_id=RELAY_ID, image_sha256=None, tool="uhubctl", verify="skipped"))
    collect_hardware_receipt(job, db=database, config=main_module.app_state.config, audit_path=audit_path)
    assert database.get_hardware_receipt(job_id)["status"] == "collected"
    _write_receipt(main_module.app_state.config.local_home_dir, job_id, _receipt(device_id=DEVICE_ID, image_sha256=None, tool="uhubctl", verify="skipped"))
    collect_hardware_receipt(job, db=database, config=main_module.app_state.config, audit_path=audit_path)
    assert database.get_hardware_receipt(job_id)["reason"] == "device_id_mismatch"
