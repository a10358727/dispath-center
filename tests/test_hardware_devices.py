"""DG-HARDWARE-EXECUTION v1 P1（H-1）：附掛裝置資源模型與在場探測。

範圍：`DeviceSpec` 宣告（封閉欄位／封閉 presence 形式／機器內 id 唯一）、
servers.yaml 載入、`validate_server_config()`／`normalize_server_config()`／
safe dict、monitor 的 `---DEVICES---` 封閉探測段（INV-SSH-4：白名單在組指令的
Python 層）、`ServerState.devices` 的 unknown-not-absent 語意（INV-SSH-7 同構）
與 `server_observations.devices_json` 落地。
"""

from types import SimpleNamespace

import pytest
import yaml

from app.config import (
    DeviceSpec,
    device_spec_errors,
    load_servers_yaml,
    parse_device_spec,
    parse_device_specs,
)
from app.db import Database
from app.monitor import (
    build_device_presence_check,
    build_probe_command,
    build_probe_command_with_devices,
    device_presence_error,
    parse_capacity_probe_output_with_devices,
    parse_device_probe_output,
    probe_server,
    ServerState,
)
from app.server_config import (
    normalize_server_config,
    server_config_to_safe_dict,
    validate_server_config,
)


# ---------------------------------------------------------------------------
# presence 封閉形式
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "presence",
    [
        "usb_vidpid:303a:1001",
        "usb_vidpid:0403:6010",
        "serial_by_id:usb-Espressif_USB_JTAG_serial_debug_unit_XX-if00",
        "path:/dev/ttyUSB0",
        "path:/dev/serial/by-path/pci-0000",
    ],
)
def test_presence_closed_forms_accepted(presence):
    assert device_presence_error(presence) is None
    command = build_device_presence_check(presence)
    assert command.startswith(("lsusb -d ", "test -e "))


@pytest.mark.parametrize(
    "presence",
    [
        "",
        None,
        "usb_vidpid:303a-1001",
        "usb_vidpid:303a:10011",
        "serial_by_id:has/slash",
        "serial_by_id:has space",
        "path:relative/path",
        "path:/dev/../etc/shadow",
        "path:/dev/tty USB0",
        "path:/dev/tty;reboot",
        "lsusb:303a:1001",
        "path:",
    ],
)
def test_presence_invalid_forms_rejected(presence):
    assert device_presence_error(presence) is not None
    with pytest.raises(ValueError):
        build_device_presence_check(presence)  # type: ignore[arg-type]


def test_presence_commands_are_exact_and_read_only():
    assert build_device_presence_check("usb_vidpid:303A:1001") == "lsusb -d 303a:1001"
    assert (
        build_device_presence_check("serial_by_id:usb-esp32-if00")
        == "test -e /dev/serial/by-id/usb-esp32-if00"
    )
    assert build_device_presence_check("path:/dev/ttyUSB0") == "test -e /dev/ttyUSB0"


# ---------------------------------------------------------------------------
# 探測指令與解析
# ---------------------------------------------------------------------------


def _esp32(device_id="esp32-1"):
    return DeviceSpec(
        id=device_id, kind="mcu", presence="usb_vidpid:303a:1001", model="ESP32-S3"
    )


def test_probe_command_without_devices_is_unchanged():
    assert build_probe_command_with_devices([]) == build_probe_command()


def test_probe_command_with_devices_appends_closed_section():
    command = build_probe_command_with_devices([_esp32(), _esp32("ftdi.2")])
    assert command.startswith(build_probe_command())
    assert "---DEVICES---" in command
    assert "if lsusb -d 303a:1001 >/dev/null 2>&1; then echo 'esp32-1 present'; else echo 'esp32-1 absent'; fi" in command
    assert "'ftdi.2 present'" in command


def test_probe_command_builder_revalidates_and_fails_closed():
    bad_id = SimpleNamespace(id="oops;rm", presence="usb_vidpid:303a:1001")
    with pytest.raises(ValueError):
        build_probe_command_with_devices([bad_id])
    bad_presence = SimpleNamespace(id="ok-1", presence="path:/dev/tty;x")
    with pytest.raises(ValueError):
        build_probe_command_with_devices([bad_presence])


def test_parse_device_probe_output_tolerates_garbage():
    parsed = parse_device_probe_output(
        "esp32-1 present\nnoise line here\nftdi.2 absent\nbad;id present\nx maybe\n"
    )
    assert parsed == {"esp32-1": "present", "ftdi.2": "absent"}


def test_parse_capacity_probe_output_with_devices_section_and_without():
    base = (
        "35, 1024, 24576\n---LOADAVG---\n0.5 0.4 0.3 1/100 999\n---DF---\n"
        "/dev/sda1 100 50 52428800 50% /\n---FREE---\nMem: 16 8 8\n---NPROC---\n8\n"
    )
    *_, devices, executables = parse_capacity_probe_output_with_devices(
        base + "---DEVICES---\nesp32-1 present\n---EXECUTABLES---\nesptool.py present\nvivado absent\n"
    )
    assert devices == {"esp32-1": "present"}
    assert executables == {"esptool.py": True, "vivado": False}
    *_, missing, missing_executables = parse_capacity_probe_output_with_devices(base)
    assert missing is None  # 未觀測＝unknown，不是 absent
    assert missing_executables is None


@pytest.mark.asyncio
async def test_probe_server_reports_devices_and_unknown_semantics():
    async def fake_ssh(_name, command, _timeout):
        assert "---DEVICES---" in command
        return SimpleNamespace(
            stdout="---LOADAVG---\n0.1 0.1 0.1 1/1 1\n---DEVICES---\nesp32-1 absent\n"
        )

    state = await probe_server(fake_ssh, "w1", devices=[_esp32()])
    assert state.online is True
    assert state.devices == {"esp32-1": "absent"}

    async def fake_ssh_plain(_name, command, _timeout):
        assert "---DEVICES---" not in command
        return SimpleNamespace(stdout="---LOADAVG---\n0.1 0.1 0.1 1/1 1\n")

    no_devices = await probe_server(fake_ssh_plain, "w1")
    assert no_devices.devices == {}  # 觀測過、無裝置可觀測

    async def boom(_name, _command, _timeout):
        raise RuntimeError("ssh down")

    offline = await probe_server(boom, "w1", devices=[_esp32()])
    assert offline.online is False
    assert offline.devices is None  # 離線＝未觀測


def test_server_state_devices_default_is_unknown():
    assert ServerState(name="w1").devices is None


# ---------------------------------------------------------------------------
# DeviceSpec 宣告驗證與載入
# ---------------------------------------------------------------------------


def test_parse_device_spec_valid_and_defaults():
    spec = parse_device_spec(
        {"id": "esp32-1", "kind": "mcu", "presence": "usb_vidpid:303a:1001"}
    )
    assert spec.model == "" and spec.serial is None and spec.tags == []
    assert spec.power_control is None


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ({"id": "a b", "kind": "mcu", "presence": "path:/dev/x"}, "devices.id"),
        ({"id": "a", "kind": "gpu", "presence": "path:/dev/x"}, "devices.kind"),
        ({"id": "a", "kind": "mcu", "presence": "nope"}, "devices.presence"),
        ({"id": "a", "kind": "mcu", "presence": "path:/dev/x", "extra": 1}, "未知欄位"),
        (
            {"id": "a", "kind": "mcu", "presence": "path:/dev/x", "power_control": "usb_relay"},
            "只有 kind=power",
        ),
        (
            {"id": "a", "kind": "power", "presence": "path:/dev/x", "power_control": "ssh"},
            "power_control",
        ),
        ({"id": "a", "kind": "mcu", "presence": "path:/dev/x", "tags": ["a;b"]}, "devices.tags"),
        ("not-a-dict", "必須是物件"),
    ],
)
def test_device_spec_errors_closed_vocabulary(raw, fragment):
    errors = device_spec_errors(raw)
    assert errors and any(fragment in error for error in errors)


def test_parse_device_specs_rejects_duplicate_ids():
    raw = [
        {"id": "b1", "kind": "mcu", "presence": "path:/dev/ttyUSB0"},
        {"id": "b1", "kind": "programmer", "presence": "path:/dev/ttyUSB1"},
    ]
    with pytest.raises(ValueError, match="重複"):
        parse_device_specs(raw, server_name="w1")


def test_load_servers_yaml_parses_devices_and_fails_fast_on_bad_declaration(tmp_path):
    good = tmp_path / "servers.yaml"
    good.write_text(
        yaml.safe_dump(
            {
                "servers": [
                    {
                        "name": "w1",
                        "host": "10.0.0.1",
                        "user": "u",
                        "key": "~/.ssh/k",
                        "devices": [
                            {
                                "id": "esp32-1",
                                "kind": "mcu",
                                "model": "ESP32-S3",
                                "presence": "usb_vidpid:303a:1001",
                                "tags": ["esp32"],
                            }
                        ],
                    }
                ]
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    servers = load_servers_yaml(good)
    assert servers[0].devices[0].id == "esp32-1"
    assert servers[0].devices[0].presence == "usb_vidpid:303a:1001"

    bad = tmp_path / "bad.yaml"
    bad.write_text(
        yaml.safe_dump(
            {
                "servers": [
                    {
                        "name": "w1",
                        "host": "10.0.0.1",
                        "user": "u",
                        "key": "~/.ssh/k",
                        "devices": [{"id": "x", "kind": "mcu", "presence": "path:../x"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_servers_yaml(bad)


# ---------------------------------------------------------------------------
# server_update 路徑：validate / normalize / safe dict
# ---------------------------------------------------------------------------


def _valid_payload(tmp_path):
    key = tmp_path / "id_ed25519"
    key.write_text("k", encoding="utf-8")
    key.chmod(0o600)
    from app.config import AppConfig

    config = AppConfig(servers=[])
    config.ssh_key_allowed_dirs = [str(tmp_path)]
    return (
        {
            "name": "w1",
            "host": "10.0.0.1",
            "user": "u",
            "key": str(key),
            "devices": [
                {"id": "esp32-1", "kind": "mcu", "presence": "usb_vidpid:303a:1001"}
            ],
        },
        config,
    )


def test_validate_server_config_accepts_and_rejects_devices(tmp_path):
    payload, config = _valid_payload(tmp_path)
    ok, errors, _ = validate_server_config(payload, config)
    assert ok, errors

    payload["devices"].append({"id": "esp32-1", "kind": "mcu", "presence": "path:/dev/x"})
    ok, errors, _ = validate_server_config(payload, config)
    assert not ok and any("重複" in error for error in errors)

    payload["devices"] = [{"id": "ok", "kind": "mcu", "presence": "path:/x", "cmd": "rm"}]
    ok, errors, _ = validate_server_config(payload, config)
    assert not ok and any("未知欄位" in error for error in errors)


def test_normalize_and_safe_dict_round_trip_devices(tmp_path):
    payload, _config = _valid_payload(tmp_path)
    normalized = normalize_server_config(payload)
    assert normalized["devices"][0]["model"] == ""
    assert normalized["devices"][0]["power_control"] is None

    from app.config import load_servers_yaml as _load  # round trip through yaml

    yaml_path = tmp_path / "servers.yaml"
    yaml_path.write_text(
        yaml.safe_dump({"servers": [normalized]}, allow_unicode=True), encoding="utf-8"
    )
    cfg = _load(yaml_path)[0]
    safe = server_config_to_safe_dict(cfg)
    assert safe["devices"] == normalized["devices"]


# ---------------------------------------------------------------------------
# 觀測落地
# ---------------------------------------------------------------------------


def test_server_observation_persists_devices_json(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    observation = db.insert_server_observation(
        server_name="w1",
        online=True,
        probe_ok=True,
        devices_json='{"esp32-1": "present"}',
    )
    assert observation.devices_json == '{"esp32-1": "present"}'
    unknown = db.insert_server_observation(server_name="w1", online=False, probe_ok=False)
    assert unknown.devices_json is None


# ---------------------------------------------------------------------------
# P1b：required_devices 需求模型、digest 穩定性、匹配與觀測資格
# ---------------------------------------------------------------------------


def test_device_requirement_model_is_closed_and_sorted():
    from app.project_bootstrap import ResourceRequirements

    requirements = ResourceRequirements.model_validate(
        {
            "required_tags": [],
            "required_devices": [
                {"kind": "mcu", "tags": ["esp32"]},
                {"kind": "fpga", "id": "board-1"},
            ],
        }
    )
    dumped = requirements.model_dump(mode="json")
    assert [d["kind"] for d in dumped["required_devices"]] == ["fpga", "mcu"]

    with pytest.raises(Exception):
        ResourceRequirements.model_validate(
            {"required_devices": [{"kind": "gpu"}]}
        )
    with pytest.raises(Exception):
        ResourceRequirements.model_validate(
            {"required_devices": [{"kind": "mcu", "id": "bad id"}]}
        )
    with pytest.raises(Exception):  # duplicates
        ResourceRequirements.model_validate(
            {"required_devices": [{"kind": "mcu"}, {"kind": "mcu"}]}
        )


def test_resource_requirements_digest_is_stable_without_devices():
    """空 required_devices 不進 dump——既有 run-template revision 的
    spec_digest／resource_requirements_digest 一個 byte 都不能變。"""

    from app.project_bootstrap import ResourceRequirements

    legacy_body = {
        "required_tags": ["gpu"],
        "min_gpu_count": 1,
        "min_gpu_memory_mb": 0,
        "min_available_ram_mb": 0,
        "min_available_disk_mb": 0,
        "exclusive_worker": True,
    }
    dumped = ResourceRequirements.model_validate(legacy_body).model_dump(mode="json")
    assert dumped == legacy_body
    assert "required_devices" not in dumped

    declared = ResourceRequirements.model_validate(
        {**legacy_body, "required_devices": [{"kind": "mcu", "tags": ["esp32"]}]}
    ).model_dump(mode="json")
    assert declared["required_devices"] == [
        {"kind": "mcu", "id": None, "tags": ["esp32"]}
    ]


def test_match_required_devices_matrix():
    from app.execution_plan_v2_store import _match_required_devices

    declared = [
        _esp32(),
        DeviceSpec(
            id="artix-1",
            kind="fpga",
            presence="usb_vidpid:0403:6010",
            tags=["artix7", "jtag"],
        ),
    ]
    declared[0].tags = ["esp32"]

    assert _match_required_devices([{"kind": "mcu"}], declared) == ("esp32-1",)
    assert _match_required_devices(
        [{"kind": "fpga", "tags": ["artix7"]}], declared
    ) == ("artix-1",)
    assert _match_required_devices(
        [{"kind": "fpga", "id": "artix-1"}, {"kind": "mcu", "tags": ["esp32"]}],
        declared,
    ) == ("artix-1", "esp32-1")

    with pytest.raises(ValueError, match="target_device_missing"):
        _match_required_devices([{"kind": "power"}], declared)
    with pytest.raises(ValueError, match="target_device_missing"):
        _match_required_devices([{"kind": "mcu", "id": "other"}], declared)
    with pytest.raises(ValueError, match="target_device_missing"):
        _match_required_devices([{"kind": "mcu", "tags": ["stm32"]}], declared)
    assert _match_required_devices([], declared) == ()


def test_resource_observation_enforces_device_presence(tmp_path):
    """同一筆新鮮觀測必須回答裝置在場：NULL/缺 id＝unknown、absent＝absent、
    present 才放行（INV-SSH-7 同構的 unknown-not-absent）。"""

    from datetime import datetime, timezone

    from app.execution_plan_v2_store import _resource_observation

    db = Database(str(tmp_path / "t.db"))
    requirements = {
        "min_gpu_count": 0,
        "min_gpu_memory_mb": 0,
        "min_available_ram_mb": 0,
        "min_available_disk_mb": 0,
        "exclusive_worker": True,
    }
    activated = "2026-08-30T00:00:00+00:00"

    def check(devices_json):
        db.insert_server_observation(
            server_name="w1",
            online=True,
            probe_ok=True,
            gpu_count=0,
            gpu_mem_used_mb=0,
            gpu_mem_total_mb=0,
            mem_available_bytes=0,
            disk_avail_bytes=0,
            devices_json=devices_json,
        )
        with db.cursor() as cur:
            return _resource_observation(
                cur,
                server_name="w1",
                activated_at=activated,
                requirements=requirements,
                now=datetime.now(timezone.utc),
                required_device_ids=("esp32-1",),
            )

    with pytest.raises(ValueError, match="target_device_observation_unknown"):
        check(None)
    with pytest.raises(ValueError, match="target_device_observation_unknown"):
        check("not-json")
    with pytest.raises(ValueError, match="target_device_observation_unknown"):
        check('{"other": "present"}')
    with pytest.raises(ValueError, match="target_device_absent"):
        check('{"esp32-1": "absent"}')
    provenance = check('{"esp32-1": "present"}')
    assert provenance.server_name == "w1"

    with db.cursor() as cur:  # 沒有裝置需求時不看 devices_json（既有語意不變）
        assert _resource_observation(
            cur,
            server_name="w1",
            activated_at=activated,
            requirements=requirements,
            now=datetime.now(timezone.utc),
        ).server_name == "w1"


# ---------------------------------------------------------------------------
# P1c：executable_present——探測、readiness 證據、resolver 開閘
# ---------------------------------------------------------------------------


def test_executable_probe_builder_is_closed_and_fails_closed():
    from app.monitor import build_probe_command_with_devices_and_executables

    command = build_probe_command_with_devices_and_executables(
        [], ["esptool.py", "openFPGALoader", "st-flash"]
    )
    assert "---EXECUTABLES---" in command
    assert (
        "if command -v esptool.py >/dev/null 2>&1; "
        "then echo 'esptool.py present'; else echo 'esptool.py absent'; fi"
    ) in command
    with pytest.raises(ValueError):
        build_probe_command_with_devices_and_executables([], ["bad name"])
    with pytest.raises(ValueError):
        build_probe_command_with_devices_and_executables([], ["-leading-dash"])
    assert build_probe_command_with_devices_and_executables([], []) == build_probe_command()


def test_parse_executable_probe_output_tolerates_garbage():
    from app.monitor import parse_executable_probe_output

    parsed = parse_executable_probe_output(
        "esptool.py present\nnoise\nvivado absent\nbad name present\n"
    )
    assert parsed == {"esptool.py": True, "vivado": False}


@pytest.mark.asyncio
async def test_probe_server_reports_executables():
    async def fake_ssh(_name, command, _timeout):
        assert "---EXECUTABLES---" in command and "command -v esptool.py" in command
        return SimpleNamespace(
            stdout=(
                "---LOADAVG---\n0.1 0.1 0.1 1/1 1\n"
                "---EXECUTABLES---\nesptool.py present\n"
            )
        )

    state = await probe_server(fake_ssh, "w1", executables=["esptool.py"])
    assert state.executables == {"esptool.py": True}

    async def fake_ssh_plain(_name, command, _timeout):
        assert "---EXECUTABLES---" not in command
        return SimpleNamespace(stdout="---LOADAVG---\n0.1 0.1 0.1 1/1 1\n")

    plain = await probe_server(fake_ssh_plain, "w1")
    assert plain.executables == {}


def test_executable_preflight_name_vocabulary():
    from app.project_bootstrap import EnvironmentPreflightCheck

    check = EnvironmentPreflightCheck.model_validate(
        {"kind": "executable_present", "name": "openFPGALoader"}
    )
    assert check.name == "openFPGALoader"
    for name in ("esptool.py", "st-flash", "vivado"):
        EnvironmentPreflightCheck.model_validate(
            {"kind": "executable_present", "name": name}
        )
    with pytest.raises(Exception):
        EnvironmentPreflightCheck.model_validate(
            {"kind": "executable_present", "name": "bad name"}
        )
    with pytest.raises(Exception):
        EnvironmentPreflightCheck.model_validate(
            {"kind": "executable_present", "name": "-dash"}
        )


def _environment(checks):
    from app.project_bootstrap import EnvironmentRevisionInput

    return EnvironmentRevisionInput.model_validate(
        {
            "name": "toolchain",
            "setup_command": "true",
            "required_server_tags": [],
            "preflight_checks": checks,
        }
    )


def test_execution_resolver_accepts_executable_checks_only():
    """DG-HARDWARE-EXECUTION v1 具名核准的 resolver 擴張：executable_present
    不再擋在計畫建立／核准；其餘三種 kind 維持拒絕。"""

    from app.execution_plan_v2_store import _validate_supported_execution_preflight

    _validate_supported_execution_preflight(
        _environment([{"kind": "executable_present", "name": "esptool.py"}])
    )
    with pytest.raises(ValueError, match="environment_preflight_evidence_unsupported"):
        _validate_supported_execution_preflight(
            _environment(
                [{"kind": "project_relative_path_present", "path": "configs/x.yaml"}]
            )
        )


def test_environment_readiness_uses_executable_evidence():
    from datetime import datetime, timezone

    from app.project_environments import (
        HostObservationEvidence,
        VerifiedHostCandidate,
        evaluate_environment_readiness,
    )

    revision = _environment([{"kind": "executable_present", "name": "esptool.py"}])
    now = datetime.now(timezone.utc)
    observed = now.isoformat()

    def candidate(executables):
        return VerifiedHostCandidate(
            tags=(),
            activated_at="2026-08-30T00:00:00+00:00",
            observation=HostObservationEvidence(
                observed_at=observed, online=True, probe_ok=True, executables=executables
            ),
        )

    ready = evaluate_environment_readiness(
        revision,
        status="approved",
        candidates=[candidate({"esptool.py": True})],
        now=now,
        stale_after_seconds=120,
    )
    assert ready["state"] == "ready"
    assert {"kind": "executable_present", "name": "esptool.py", "path": None,
            "state": "satisfied"} in ready["checks"]

    missing = evaluate_environment_readiness(
        revision,
        status="approved",
        candidates=[candidate({"esptool.py": False})],
        now=now,
        stale_after_seconds=120,
    )
    assert missing["state"] == "not_ready"
    assert "executable_missing" in missing["reasons"]
    assert missing["checks"][0]["state"] == "missing"

    unknown = evaluate_environment_readiness(
        revision,
        status="approved",
        candidates=[candidate(None)],
        now=now,
        stale_after_seconds=120,
    )
    assert unknown["state"] == "unknown"
    assert unknown["checks"][0]["state"] == "unknown"


def test_declared_executable_names_come_from_approved_heads(tmp_path):
    import json
    import sqlite3

    db = Database(str(tmp_path / "t.db"))
    raw = sqlite3.connect(str(tmp_path / "t.db"))  # FK 預設 off：直接鋪 scaffolding
    raw.row_factory = sqlite3.Row

    def insert(env, rev, status, checks):
        raw.execute(
            """
            INSERT INTO environment_revisions
                (id, environment_id, project_id, revision, status, contract_version,
                 setup_command, required_server_tags_json, working_directory_policy,
                 non_secret_env_json, secret_references_json, preflight_checks_json,
                 revision_digest, supersedes_id, approval_id, created_by_actor_id,
                 created_at)
            VALUES (?, ?, ?, ?, ?, 'host-environment-v1', 'true', '[]',
                    'project_checkout', '[]', '[]', ?, ?, NULL, 1, 'actor',
                    '2026-08-31T00:00:00+00:00')
            """,
            (
                f"00000000-0000-0000-0000-0000000000{env}{rev}",
                f"e-{env}",
                "p-1",
                rev,
                status,
                json.dumps(checks),
                "0" * 64,
            ),
        )

    def exe(name):
        return {"kind": "executable_present", "name": name}

    insert("1", 1, "approved", [exe("esptool.py")])  # 非 head：不算
    insert("1", 2, "approved", [exe("openFPGALoader"), exe("st-flash"),
                                 {"kind": "server_tag_present", "name": "gpu"}])
    insert("2", 1, "archived", [exe("vivado")])  # archived head：不算
    raw.commit()
    raw.close()

    assert db.list_declared_executable_preflight_names() == (
        "openFPGALoader",
        "st-flash",
    )
    assert db.list_declared_executable_preflight_names(limit=1) == ("openFPGALoader",)


def test_server_observation_persists_executables_json(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    observation = db.insert_server_observation(
        server_name="w1",
        online=True,
        probe_ok=True,
        executables_json='{"vivado": false}',
    )
    assert observation.executables_json == '{"vivado": false}'
