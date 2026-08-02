"""app/server_config.py 單元測試（PLAN.md I.4，階段 8 第二批）。

涵蓋 I.11 測試清單：
- validate_server_config() 逐條規則（合法通過；name/host/user/key 各種
  否定案例；project_roots/dataset_roots 禁止路徑；key 權限過寬 warning）
- atomic write 正確性（寫入後讀回內容一致，暫存檔不殘留）
- backup 產生
"""

from __future__ import annotations

import asyncio
import os

import pytest

from app.config import AppConfig
from app.server_config import (
    backup_servers_yaml,
    load_servers_config,
    normalize_server_config,
    server_config_to_safe_dict,
    validate_server_config,
    write_servers_yaml_atomically,
)
# 別名匯入：pytest 會把模組頂層任何 `test_` 開頭的可呼叫物件當測試項目收集，
# 直接 `from ... import test_ssh_connection` 會被誤判成一個測試函式（且缺少
# 對應 fixture 而報錯），因此改名匯入避免撞名。
from app.server_config import test_ssh_connection as check_ssh_connection


def _config(**overrides) -> AppConfig:
    base = dict(servers=[], allow_root_ssh=False, ssh_key_allowed_dirs=["~/.ssh"])
    base.update(overrides)
    return AppConfig(**base)


def _make_key_file(tmp_path, name="id_test", mode=0o600) -> str:
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    key_path = ssh_dir / name
    key_path.write_text("fake-private-key-content\n")
    os.chmod(key_path, mode)
    return str(key_path)


def _valid_payload(tmp_path, **overrides) -> dict:
    payload = {
        "name": "server-x",
        "host": "10.0.0.5",
        "user": "train",
        "key": _make_key_file(tmp_path),
        "port": 22,
        "tags": ["gpu"],
        "project_roots": ["~/projects"],
        "dataset_roots": ["~/datasets"],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# validate_server_config()：合法案例
# ---------------------------------------------------------------------------


def test_validate_server_config_accepts_valid_payload(tmp_path):
    payload = _valid_payload(tmp_path)
    config = _config(ssh_key_allowed_dirs=[str(tmp_path / ".ssh")])
    ok, errors, warnings = validate_server_config(payload, config)
    assert ok is True
    assert errors == []
    assert warnings == []


@pytest.mark.parametrize("bad_name", ["bad name", "bad/name", "bad;name", "bad$(name)", ""])
def test_validate_server_config_rejects_bad_name(tmp_path, bad_name):
    payload = _valid_payload(tmp_path, name=bad_name)
    config = _config(ssh_key_allowed_dirs=[str(tmp_path / ".ssh")])
    ok, errors, _warnings = validate_server_config(payload, config)
    assert ok is False
    assert errors


def test_validate_server_config_rejects_host_0000(tmp_path):
    payload = _valid_payload(tmp_path, host="0.0.0.0")
    config = _config(ssh_key_allowed_dirs=[str(tmp_path / ".ssh")])
    ok, errors, _warnings = validate_server_config(payload, config)
    assert ok is False
    assert any("0.0.0.0" in e for e in errors)


def test_validate_server_config_rejects_root_user_without_allow_root_ssh(tmp_path):
    payload = _valid_payload(tmp_path, user="root")
    config = _config(ssh_key_allowed_dirs=[str(tmp_path / ".ssh")], allow_root_ssh=False)
    ok, errors, _warnings = validate_server_config(payload, config)
    assert ok is False
    assert any("root" in e for e in errors)


def test_validate_server_config_allows_root_user_with_allow_root_ssh(tmp_path):
    payload = _valid_payload(tmp_path, user="root")
    config = _config(ssh_key_allowed_dirs=[str(tmp_path / ".ssh")], allow_root_ssh=True)
    ok, errors, _warnings = validate_server_config(payload, config)
    assert ok is True
    assert errors == []


def test_validate_server_config_rejects_pub_key(tmp_path):
    key_path = _make_key_file(tmp_path, name="id_test.pub")
    payload = _valid_payload(tmp_path, key=key_path)
    config = _config(ssh_key_allowed_dirs=[str(tmp_path / ".ssh")])
    ok, errors, _warnings = validate_server_config(payload, config)
    assert ok is False
    assert any(".pub" in e for e in errors)


def test_validate_server_config_rejects_missing_key_file(tmp_path):
    payload = _valid_payload(tmp_path, key=str(tmp_path / ".ssh" / "does-not-exist"))
    config = _config(ssh_key_allowed_dirs=[str(tmp_path / ".ssh")])
    ok, errors, _warnings = validate_server_config(payload, config)
    assert ok is False
    assert any("不存在" in e for e in errors)


def test_validate_server_config_rejects_key_outside_allowed_dirs(tmp_path):
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    key_path = outside_dir / "id_outside"
    key_path.write_text("fake\n")
    os.chmod(key_path, 0o600)
    payload = _valid_payload(tmp_path, key=str(key_path))
    config = _config(ssh_key_allowed_dirs=[str(tmp_path / ".ssh")])
    ok, errors, _warnings = validate_server_config(payload, config)
    assert ok is False
    assert any("允許的目錄" in e for e in errors)


def test_validate_server_config_rejects_key_path_traversal(tmp_path):
    """`../` 繞過：key 路徑用 `..` 試圖跳出允許目錄，realpath 正規化後應該
    被擋下。"""
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    key_path = outside_dir / "id_outside"
    key_path.write_text("fake\n")
    os.chmod(key_path, 0o600)

    allowed_dir = tmp_path / ".ssh"
    allowed_dir.mkdir()
    traversal_path = str(allowed_dir / ".." / "outside" / "id_outside")

    payload = _valid_payload(tmp_path, key=traversal_path)
    config = _config(ssh_key_allowed_dirs=[str(allowed_dir)])
    ok, errors, _warnings = validate_server_config(payload, config)
    assert ok is False
    assert any("允許的目錄" in e for e in errors)


@pytest.mark.parametrize("bad_root", ["/", "~", "/home", "/etc", "/tmp"])
def test_validate_server_config_rejects_forbidden_project_root(tmp_path, bad_root):
    payload = _valid_payload(tmp_path, project_roots=[bad_root])
    config = _config(ssh_key_allowed_dirs=[str(tmp_path / ".ssh")])
    ok, errors, _warnings = validate_server_config(payload, config)
    assert ok is False
    assert any("禁止掃描" in e for e in errors)


@pytest.mark.parametrize("good_root", ["~/foo", "/data/x"])
def test_validate_server_config_accepts_allowed_project_root(tmp_path, good_root):
    payload = _valid_payload(tmp_path, project_roots=[good_root])
    config = _config(ssh_key_allowed_dirs=[str(tmp_path / ".ssh")])
    ok, errors, _warnings = validate_server_config(payload, config)
    assert ok is True
    assert errors == []


def test_validate_server_config_rejects_forbidden_dataset_root(tmp_path):
    payload = _valid_payload(tmp_path, dataset_roots=["/etc"])
    config = _config(ssh_key_allowed_dirs=[str(tmp_path / ".ssh")])
    ok, errors, _warnings = validate_server_config(payload, config)
    assert ok is False
    assert any("禁止掃描" in e for e in errors)


def test_validate_server_config_rejects_bad_port(tmp_path):
    payload = _valid_payload(tmp_path, port=70000)
    config = _config(ssh_key_allowed_dirs=[str(tmp_path / ".ssh")])
    ok, errors, _warnings = validate_server_config(payload, config)
    assert ok is False
    assert any("port" in e for e in errors)


def test_validate_server_config_rejects_tags_with_forbidden_chars(tmp_path):
    payload = _valid_payload(tmp_path, tags=["a b"])
    config = _config(ssh_key_allowed_dirs=[str(tmp_path / ".ssh")])
    ok, errors, _warnings = validate_server_config(payload, config)
    assert ok is False
    assert any("tags" in e for e in errors)


def test_validate_server_config_key_permission_too_open_is_warning_not_error(tmp_path):
    # 用跟預設不同的檔名，避免 _valid_payload() 內部再呼叫一次
    # _make_key_file(tmp_path)（預設檔名 id_test、mode 600）覆蓋掉這裡刻意
    # 設成過寬權限的同名檔案。
    key_path = _make_key_file(tmp_path, name="id_loose", mode=0o644)
    payload = _valid_payload(tmp_path, key=key_path)
    config = _config(ssh_key_allowed_dirs=[str(tmp_path / ".ssh")])
    ok, errors, warnings = validate_server_config(payload, config)
    assert ok is True
    assert errors == []
    assert any("權限過寬" in w for w in warnings)


# ---------------------------------------------------------------------------
# normalize_server_config()
# ---------------------------------------------------------------------------


def test_normalize_server_config_fills_defaults(tmp_path):
    payload = {"name": "server-x", "host": "10.0.0.5", "user": "train", "key": "~/.ssh/id_rsa"}
    normalized = normalize_server_config(payload)
    assert normalized["port"] == 22
    assert normalized["tags"] == []
    assert normalized["project_roots"] == []
    assert normalized["dataset_roots"] == []
    assert normalized["enabled"] is True
    assert normalized["note"] is None
    assert "data" in normalized["project_embedded_dataset_names"]
    assert ".git" in normalized["project_exclude_names"]


# ---------------------------------------------------------------------------
# atomic write / backup
# ---------------------------------------------------------------------------


def test_write_servers_yaml_atomically_writes_readable_content_and_no_tmp_left(tmp_path):
    path = str(tmp_path / "servers.yaml")
    config_dict = {"servers": [{"name": "server-x", "host": "10.0.0.5", "user": "train", "key": "~/.ssh/id_rsa"}]}
    write_servers_yaml_atomically(path, config_dict)

    loaded = load_servers_config(path)
    assert loaded == config_dict

    leftover = list(tmp_path.glob("servers.yaml.tmp.*"))
    assert leftover == []


def test_load_servers_config_missing_file_returns_empty_servers(tmp_path):
    path = str(tmp_path / "does-not-exist.yaml")
    assert load_servers_config(path) == {"servers": []}


def test_backup_servers_yaml_creates_copy(tmp_path):
    path = str(tmp_path / "servers.yaml")
    write_servers_yaml_atomically(path, {"servers": []})

    backup_path = backup_servers_yaml(path)
    assert backup_path is not None
    assert os.path.exists(backup_path)
    assert backup_path.startswith(path + ".bak.")


def test_backup_servers_yaml_missing_file_returns_none(tmp_path):
    path = str(tmp_path / "does-not-exist.yaml")
    assert backup_servers_yaml(path) is None


# ---------------------------------------------------------------------------
# server_config_to_safe_dict()：不洩漏 key 內容
# ---------------------------------------------------------------------------


def test_server_config_to_safe_dict_only_shows_key_path(tmp_path):
    from app.config import ServerConfig

    key_path = _make_key_file(tmp_path)
    cfg = ServerConfig(name="server-x", host="10.0.0.5", user="train", key=key_path)
    d = server_config_to_safe_dict(cfg)
    assert d["key"] == key_path
    assert d["execution_backend"] == "ssh"
    assert "fake-private-key-content" not in str(d)


# ---------------------------------------------------------------------------
# test_ssh_connection()：只跑固定六類指令
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, stdout: str):
        self.stdout = stdout


def test_test_ssh_connection_runs_only_fixed_commands(tmp_path):
    from app.config import ServerConfig

    cfg = ServerConfig(
        name="server-x",
        host="10.0.0.5",
        user="train",
        key=_make_key_file(tmp_path),
        project_roots=["~/projects"],
        dataset_roots=["/data/sets"],
    )
    calls: list[str] = []

    async def fake_ssh_run(server_name, command, timeout):
        calls.append(command)
        if command == "hostname":
            return _FakeResult("server-x-host\n")
        if command == "whoami":
            return _FakeResult("train\n")
        if "test -d" in command:
            return _FakeResult("exists\n")
        return _FakeResult("")

    result = asyncio.run(check_ssh_connection(cfg, fake_ssh_run))
    assert result["ok"] is True
    assert result["results"]["hostname"] == "server-x-host"
    assert result["results"]["whoami"] == "train"
    assert result["results"]["project_roots"]["~/projects"] == "exists"
    assert result["results"]["dataset_roots"]["/data/sets"] == "exists"

    # 只有固定六類指令：hostname/whoami/tmux/gpu + 各一個 project_root/dataset_root
    assert len(calls) == 6
    assert "hostname" in calls
    assert "whoami" in calls
    assert any("tmux -V" in c for c in calls)
    assert any("nvidia-smi" in c for c in calls)
    assert any("test -d" in c for c in calls)


def test_test_ssh_connection_unreachable_returns_ok_false_not_exception():
    from app.config import ServerConfig

    cfg = ServerConfig(name="server-x", host="10.0.0.5", user="train", key="~/.ssh/id_rsa")

    async def failing_ssh_run(server_name, command, timeout):
        raise RuntimeError("SSH connection refused")

    result = asyncio.run(check_ssh_connection(cfg, failing_ssh_run))
    assert result["ok"] is False
    assert result["errors"]
