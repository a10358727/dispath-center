"""Web Server Management：servers.yaml 驗證、atomic write、備份、reload、
test-ssh（PLAN.md I.4，階段 8 第二批）。

鐵律（違反＝實作錯誤）：
- **這個模組本身不會讓 servers.yaml 被直接改動**：`write_servers_yaml_atomically()`
  只被 `app/approvals.py` 的 `approve()`（server_add/server_update/
  server_disable/server_delete 分支）呼叫，任何建立請求（add-request 等）
  都只走 approval，不會呼叫這裡的寫入函式。
- `validate_server_config()` 全程只用 `os.path.exists()`/`os.stat()` 檢查私
  鑰，**絕不 `open()` 讀取金鑰內容**。
- `project_roots`/`dataset_roots` 的危險路徑判斷直接重用
  `app.inventory.is_forbidden_root()`（兩級規則：系統目錄精確符合與子路徑
  都擋，`/home`／`~` 只擋精確符合本身），不另外重寫一份判斷邏輯。
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from app.config import AppConfig, ServerConfig, load_servers_yaml
from app.inventory import (
    DEFAULT_EMBEDDED_DATASET_NAMES,
    DEFAULT_EXCLUDE_NAMES,
    _quote_remote_path,
    is_forbidden_root,
)
from app.monitor import ServerState

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
#: tags 簡單防注入：不含空白、分號、`$(`（同 PLAN.md I.4 說明，不用太嚴格）。
_TAG_FORBIDDEN_CHARS = (" ", ";", "$(")


# ---------------------------------------------------------------------------
# 讀取原始 dict 結構（給 atomic write 用，不是 ServerConfig 物件列表）
# ---------------------------------------------------------------------------


def load_servers_config(path: str) -> dict:
    """讀 servers.yaml 原始結構（`{"servers": [...]}`）。檔案不存在時回傳
    `{"servers": []}`（理論上不會發生，servers.yaml 在部署時應該已存在）。"""
    p = Path(path)
    if not p.exists():
        return {"servers": []}
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("servers", [])
    return data


def server_config_to_safe_dict(cfg: ServerConfig) -> dict:
    """`ServerConfig` -> 可以回傳給前端/agent 工具的安全 dict：`key` 只是
    servers.yaml 本來就存的路徑字串本身，**不讀取、不回傳私鑰檔案內容**，
    也不回傳任何 `.env` 內容。`app/main.py`（`GET /server-config*`）與
    `app/agent_tools.py`（`list_server_configs`/`get_server_config`）共用
    這份轉換邏輯，避免兩處各自維護一份容易漂移的欄位清單（同
    `app.approvals.approval_to_dict()` 的既有模式）。"""
    return {
        "name": cfg.name,
        "host": cfg.host,
        "user": cfg.user,
        "key": cfg.key,
        "port": cfg.port,
        "gpu": cfg.gpu,
        "idle_gpu_util": cfg.idle_gpu_util,
        "idle_load": cfg.idle_load,
        "tags": list(cfg.tags),
        "project_roots": list(cfg.project_roots),
        "dataset_roots": list(cfg.dataset_roots),
        "project_embedded_dataset_names": list(cfg.project_embedded_dataset_names),
        "project_exclude_names": list(cfg.project_exclude_names),
        "enabled": cfg.enabled,
        "note": cfg.note,
        "execution_backend": cfg.execution_backend,
    }


# ---------------------------------------------------------------------------
# 驗證
# ---------------------------------------------------------------------------


def validate_server_config(payload: dict, config: AppConfig) -> tuple[bool, list[str], list[str]]:
    """驗證一筆 server 設定 payload，回傳 `(ok, errors, warnings)`。

    - `errors` 非空 → `ok=False`，呼叫端應該拒絕（不建立/不落地 approval）。
    - `warnings` 只是提醒（例如 key 檔案權限過寬），不影響 `ok`。
    """
    errors: list[str] = []
    warnings: list[str] = []

    name = payload.get("name")
    if not name or not isinstance(name, str) or not _NAME_RE.match(name):
        errors.append("name 必須是非空字串，且只能包含英數字、底線（_）、連字號（-）")

    host = payload.get("host")
    if not host or not isinstance(host, str) or not host.strip():
        errors.append("host 不可為空")
    elif host.strip() == "0.0.0.0":
        errors.append("host 不可為 0.0.0.0")

    user = payload.get("user")
    if not user or not isinstance(user, str) or not user.strip():
        errors.append("user 不可為空")
    elif user.strip() == "root" and not config.allow_root_ssh:
        errors.append("user 不可為 root（如確實需要，請在 .env 設定 ALLOW_ROOT_SSH=true）")

    key = payload.get("key")
    if not key or not isinstance(key, str) or not key.strip():
        errors.append("key 不可為空")
    else:
        expanded = os.path.expanduser(key)
        if key.rstrip("/").endswith(".pub"):
            errors.append("key 不可以是 .pub（公鑰），必須指向私鑰檔案路徑")
        if not os.path.exists(expanded):
            errors.append(f"key 檔案不存在：{key}")
        else:
            real_key = os.path.realpath(expanded)
            allowed_dirs = config.ssh_key_allowed_dirs or ["~/.ssh"]
            allowed_ok = False
            for d in allowed_dirs:
                real_dir = os.path.realpath(os.path.expanduser(d))
                if real_key == real_dir or real_key.startswith(real_dir.rstrip("/") + os.sep):
                    allowed_ok = True
                    break
            if not allowed_ok:
                errors.append(
                    f"key 檔案必須位於允許的目錄底下（{', '.join(allowed_dirs)}），"
                    "以避免使用者透過 ../ 之類的路徑指向系統其他敏感檔案"
                )
            else:
                try:
                    mode = os.stat(real_key).st_mode
                    if mode & 0o077:
                        warnings.append(f"key 檔案權限過寬（建議 chmod 600）：{key}")
                except OSError:
                    pass

    port = payload.get("port", 22)
    try:
        port_int = int(port)
        if not (1 <= port_int <= 65535):
            errors.append("port 必須介於 1-65535 之間")
    except (TypeError, ValueError):
        errors.append("port 必須是整數")

    for field_name in ("project_roots", "dataset_roots"):
        values = payload.get(field_name) or []
        if not isinstance(values, list):
            errors.append(f"{field_name} 必須是字串列表")
            continue
        for v in values:
            if not isinstance(v, str) or not v.strip():
                errors.append(f"{field_name} 內含不合法的路徑：{v!r}")
                continue
            if is_forbidden_root(v):
                errors.append(f"{field_name} 內含禁止掃描的路徑：{v}")

    tags = payload.get("tags") or []
    if not isinstance(tags, list):
        errors.append("tags 必須是字串列表")
    else:
        for t in tags:
            if not isinstance(t, str) or not t.strip():
                errors.append(f"tags 內含不合法的項目：{t!r}")
                continue
            if any(ch in t for ch in _TAG_FORBIDDEN_CHARS):
                errors.append(f"tags 內含不允許的字元：{t!r}")

    return (len(errors) == 0, errors, warnings)


# ---------------------------------------------------------------------------
# 補齊預設值 / 型別轉換
# ---------------------------------------------------------------------------


def normalize_server_config(payload: dict) -> dict:
    """補齊預設值、型別轉換。輸入已經通過 `validate_server_config()`。"""
    normalized = dict(payload)
    normalized["port"] = int(normalized.get("port", 22) or 22)
    normalized.setdefault("gpu", False)
    normalized["gpu"] = bool(normalized["gpu"])
    normalized["idle_gpu_util"] = float(normalized.get("idle_gpu_util", 15.0) or 15.0)
    normalized["idle_load"] = float(normalized.get("idle_load", 2.0) or 2.0)
    normalized.setdefault("tags", [])
    normalized["tags"] = list(normalized["tags"] or [])
    normalized.setdefault("project_roots", [])
    normalized["project_roots"] = list(normalized["project_roots"] or [])
    normalized.setdefault("dataset_roots", [])
    normalized["dataset_roots"] = list(normalized["dataset_roots"] or [])
    normalized.setdefault(
        "project_embedded_dataset_names", list(DEFAULT_EMBEDDED_DATASET_NAMES)
    )
    normalized["project_embedded_dataset_names"] = list(
        normalized["project_embedded_dataset_names"] or DEFAULT_EMBEDDED_DATASET_NAMES
    )
    normalized.setdefault("project_exclude_names", list(DEFAULT_EXCLUDE_NAMES))
    normalized["project_exclude_names"] = list(
        normalized["project_exclude_names"] or DEFAULT_EXCLUDE_NAMES
    )
    normalized.setdefault("enabled", True)
    normalized["enabled"] = bool(normalized["enabled"])
    normalized.setdefault("note", None)
    return normalized


# ---------------------------------------------------------------------------
# atomic write / backup
# ---------------------------------------------------------------------------


def write_servers_yaml_atomically(path: str, config_dict: dict) -> None:
    """寫到同目錄的暫存檔（`{path}.tmp.{pid}`）後 `os.replace()` 原子覆蓋，
    避免寫到一半被讀到（或服務崩潰時留下半個檔案）。"""
    tmp_path = f"{path}.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(config_dict, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp_path, path)


def backup_servers_yaml(path: str) -> Optional[str]:
    """複製一份到 `{path}.bak.{timestamp}`，回傳備份路徑。檔案不存在時
    （理論上不會發生）跳過，回傳 None。"""
    if not os.path.exists(path):
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup_path = f"{path}.bak.{ts}"
    shutil.copy2(path, backup_path)
    return backup_path


# ---------------------------------------------------------------------------
# reload：in-memory 熱替換（不需要 requires_restart，見模組/PLAN.md 說明）
# ---------------------------------------------------------------------------


def reload_server_config_if_supported(app_state: Any) -> dict:
    """重新 `load_servers_yaml()` 並替換 `app_state.server_configs`；新出現的
    機器名補一筆初始 `ServerState`。monitor/scheduler 迴圈本來就是讀
    `app_state.server_configs`/`server_states` 這兩個 in-memory dict，換掉
    內容即可即時生效。"""
    servers = load_servers_yaml(app_state.config.servers_yaml_path)
    new_configs = {s.name: s for s in servers}
    app_state.server_configs = new_configs
    app_state.config.servers = servers
    for name in new_configs:
        if name not in app_state.server_states:
            app_state.server_states[name] = ServerState(name=name, online=False)
    #: 從 servers.yaml 消失的機器：清掉它的 `server_states` 快取。
    #:
    #: 這裡原本刻意不清，理由是「disable/delete 都只設 enabled=false，理論上
    #: 不會從清單消失」。2026-07-26 起 `server_delete` 會真的移除整筆，那個
    #: 前提不再成立——不清的話 `GET /servers` 會繼續回報一台設定檔裡已經
    #: 不存在的機器，使用者按了刪除卻看到它還在（正是改真刪除要解決的症狀）。
    #:
    #: 安全性：`server_states` 是可拋棄的執行期快取（INV-STATE-1），持久真相
    #: 是 servers.yaml 與 jobqueue.db；而且刪除路徑本身已經擋掉「該機器有
    #: running job」的情況，不會清掉正在被使用的機器狀態。
    removed = [name for name in app_state.server_states if name not in new_configs]
    for name in removed:
        del app_state.server_states[name]
    # Preserve the public reload response contract.  ``removed`` is an internal
    # cache-maintenance detail; exposing it here changes exact API responses and
    # also leaks into every approval result that embeds ``reload``.
    return {"ok": True}


# ---------------------------------------------------------------------------
# test-ssh：唯讀，只跑六類固定指令
# ---------------------------------------------------------------------------


def _test_dir_command(path: str) -> str:
    quoted = _quote_remote_path(path)
    return f"test -d {quoted} && echo exists || echo missing"


async def test_ssh_connection(server_cfg: ServerConfig, ssh_run) -> dict:
    """只跑固定六類唯讀指令：hostname/whoami/`tmux -V || true`/
    `nvidia-smi --query-gpu=... || true`/對每個 project_root 與 dataset_root
    各一次 `test -d`。**不寫遠端檔案、不建 agent_jobs、不 kill tmux、不改
    servers.yaml**。SSH 連不上時 `ok=False`，`errors` 記錄原因，不丟例外。
    """
    results: dict[str, Any] = {}
    warnings: list[str] = []
    errors: list[str] = []
    ok = True

    fixed_commands = {
        "hostname": "hostname",
        "whoami": "whoami",
        "tmux": "tmux -V || true",
        "gpu": "nvidia-smi --query-gpu=index,name --format=csv,noheader || true",
    }
    for key, cmd in fixed_commands.items():
        try:
            res = await ssh_run(server_cfg.name, cmd, 15)
            results[key] = (res.stdout or "").strip()
        except Exception as exc:  # noqa: BLE001 - SSH 連不上等，記錄後繼續其他探測
            ok = False
            errors.append(f"{key} 失敗：{exc}")
            results[key] = None

    for label, roots in (
        ("project_roots", server_cfg.project_roots),
        ("dataset_roots", server_cfg.dataset_roots),
    ):
        entry: dict[str, str] = {}
        for root in roots:
            try:
                res = await ssh_run(server_cfg.name, _test_dir_command(root), 15)
                entry[root] = (res.stdout or "").strip() or "missing"
            except Exception as exc:  # noqa: BLE001
                ok = False
                errors.append(f"{label} {root} 檢查失敗：{exc}")
                entry[root] = "unknown"
        results[label] = entry

    return {"ok": ok, "results": results, "warnings": warnings, "errors": errors}
