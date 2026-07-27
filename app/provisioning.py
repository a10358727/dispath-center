"""Goal 3 Phase B（docs/GOAL_3_FUTURE_WORK_PLAN.md；DG-B 於 docs/DECISIONS.md
2026-07-19 具名核准）：空伺服器 provisioning——**無常駐 agent**、一次性 SSH。

邊界（DG-B 原文，實作不得放寬）：

- bootstrap 腳本是**審閱過的固定版本**（本模組常數＋SHA-256 pin），approval
  payload 綁定請求當下的 SHA；核准時腳本已改版 → 拒絕，不執行新腳本。
- **非 root、只動使用者層、冪等可重跑**。系統工具（tmux/rsync/git）DG-B
  明確排除 sudo/apt 層安裝，因此腳本對它們**只驗證存在與否**並如實回報
  `missing`——絕不嘗試提權安裝；使用者級 Python venv 則真的建立/重用
  （`~/.dispatch-center/venv`，純使用者層）。GPU 驅動完全不在範圍內。
- 腳本經既有 SFTP 路徑（`sshpool.write_file()`）遞送後以固定路徑執行，
  元件參數全部來自 `BOOTSTRAP_ALLOWED_COMPONENTS` 白名單——使用者輸入
  永遠不會被拼進 shell 指令（INV-SSH 非插值慣例）。
- 報告 fail-closed：解析不到報告、元件狀態未知、capability check 缺項，
  一律 `passed=False`；「執行了但失敗」與「連不上」是兩回事——後者丟
  例外讓 approval 維持 pending 可重試（unreachable ≠ failed）。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Awaitable, Callable, Optional

from app.config import ServerConfig

BOOTSTRAP_SCRIPT_VERSION = "v1"

#: DG-B 核准的元件全集。`python-venv` 是唯一會真正「安裝」東西的元件
#: （純使用者層 venv）；其餘三個只驗證存在。順序即腳本執行/回報順序。
BOOTSTRAP_ALLOWED_COMPONENTS = ("tmux", "rsync", "git", "python-venv")

#: 遠端存放路徑（home-相對，SFTP 慣例；見 `sshpool.write_file()`）。
BOOTSTRAP_REMOTE_DIR = ".dispatch-center"
BOOTSTRAP_REMOTE_PATH = f"{BOOTSTRAP_REMOTE_DIR}/bootstrap_{BOOTSTRAP_SCRIPT_VERSION}.sh"

#: 審閱過的固定腳本本體。**改動這個常數 = 改版**：SHA 會變，所有既有
#: pending `server_bootstrap` approval 會在核准時因 SHA 不符被拒絕，
#: 必須重新建立請求（這是刻意的——操作者核准的是「這一版腳本」）。
BOOTSTRAP_SCRIPT = """#!/usr/bin/env bash
# dispatch-center server bootstrap v1 — DG-B approved 2026-07-19.
# Non-root, user-level only, idempotent. System tools are VERIFIED, never
# installed (DG-B excludes sudo/apt); python-venv creates/reuses a
# user-level venv at ~/.dispatch-center/venv. No GPU drivers, ever.
set -u
umask 077
overall=0
items=""
for component in "$@"; do
  case "$component" in
    tmux|rsync|git)
      if command -v "$component" >/dev/null 2>&1; then
        state="present"
      else
        state="missing"
        overall=1
      fi
      ;;
    python-venv)
      if ! command -v python3 >/dev/null 2>&1; then
        state="missing"
        overall=1
      elif [ -x "$HOME/.dispatch-center/venv/bin/python" ]; then
        state="ready"
      elif python3 -m venv "$HOME/.dispatch-center/venv" >/dev/null 2>&1; then
        state="ready"
      else
        state="failed"
        overall=1
      fi
      ;;
    *)
      state="unsupported"
      overall=1
      ;;
  esac
  items="${items}\\"${component}\\":\\"${state}\\","
done
printf 'BOOTSTRAP_REPORT:{%s}\\n' "${items%,}"
exit "$overall"
"""


def bootstrap_script_sha256() -> str:
    """目前腳本常數的 SHA-256（request 時 pin 進 payload、approve 時比對）。"""

    return hashlib.sha256(BOOTSTRAP_SCRIPT.encode("utf-8")).hexdigest()


def validate_bootstrap_components(components: Any) -> list[str]:
    """驗證元件清單：非空、每項都在 DG-B 白名單、去重、按白名單順序回傳
    （決定性輸出——同輸入永遠同輸出，方便 payload 比對與測試）。不合法丟
    `ValueError`。"""

    if not isinstance(components, (list, tuple)) or not components:
        raise ValueError("components 必須是非空清單")
    requested = set()
    for item in components:
        if not isinstance(item, str) or item not in BOOTSTRAP_ALLOWED_COMPONENTS:
            raise ValueError(
                f"不允許的元件 {item!r}；DG-B 允許：{', '.join(BOOTSTRAP_ALLOWED_COMPONENTS)}"
            )
        requested.add(item)
    return [c for c in BOOTSTRAP_ALLOWED_COMPONENTS if c in requested]


def build_bootstrap_command(components: list[str]) -> str:
    """組出遠端執行指令（純函式）。元件先過 `validate_bootstrap_components`
    白名單，因此拼接是安全的——這裡沒有任何使用者自由文字。"""

    ordered = validate_bootstrap_components(components)
    return f"bash '{BOOTSTRAP_REMOTE_PATH}' " + " ".join(ordered)


_REPORT_RE = re.compile(r"^BOOTSTRAP_REPORT:(\{.*\})\s*$", re.MULTILINE)

#: 各元件「合格」狀態；其餘一律視為不合格（fail-closed）。
_COMPONENT_OK_STATES = frozenset({"present", "ready"})


def parse_bootstrap_report(stdout: str) -> Optional[dict[str, str]]:
    """從腳本輸出取**最後一個** `BOOTSTRAP_REPORT:{...}` 並解析。找不到、
    JSON 壞掉、值不是字串 → `None`（呼叫端視為 bootstrap 失敗）。"""

    matches = _REPORT_RE.findall(stdout or "")
    if not matches:
        return None
    try:
        parsed = json.loads(matches[-1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict) or not parsed:
        return None
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()):
        return None
    return parsed


#: B2 read-only capability check：與 `onboard-worker.sh` 第 3 步同款檢查。
#: bash 用 `command -v bash`（透過 SSH 執行本身就證明 shell 可用，但仍
#: 顯式回報）；GPU 機另加 nvidia-smi。全部唯讀。
CAPABILITY_CHECK_COMMANDS: dict[str, str] = {
    "bash": "command -v bash || true",
    "tmux": "command -v tmux || true",
    "rsync": "command -v rsync || true",
    "git": "command -v git || true",
    "python3": "command -v python3 || true",
}
CAPABILITY_CHECK_GPU_COMMAND = "command -v nvidia-smi || true"

#: capability check 必須全部通過的工具（GPU 只在 payload 標記 gpu 時必須）。
CAPABILITY_REQUIRED = ("bash", "tmux", "rsync")


def evaluate_capabilities(
    findings: dict[str, Optional[str]], *, gpu: bool
) -> tuple[bool, list[str]]:
    """純函式：`command -v` 輸出（None＝該次檢查失敗）→ (是否通過, 缺項清單)。
    未知（None）不等於存在——一律計為缺項（fail-closed）。"""

    missing: list[str] = []
    required = list(CAPABILITY_REQUIRED) + (["nvidia-smi"] if gpu else [])
    for tool in required:
        value = findings.get(tool)
        if not value or not value.strip():
            missing.append(tool)
    return (not missing), missing


async def run_server_bootstrap(
    server_cfg: ServerConfig,
    *,
    ssh_run_direct: Callable[..., Awaitable[Any]],
    write_file_direct: Callable[..., Awaitable[None]],
    components: list[str],
    gpu: bool = False,
    command_timeout: float = 120.0,
) -> dict[str, Any]:
    """上傳固定腳本 → 執行 → 解析報告 → B2 capability check。

    連線層例外（SSH 連不上、逾時）**原樣往上丟**——呼叫端（approve 分支）
    讓 approval 維持 pending 可重試；只有「連上且跑完」才會回報告 dict：

    ``{"script_version", "script_sha256", "components": {name: state},
       "capabilities": {tool: path|None}, "passed": bool, "errors": [...]}``
    """

    ordered = validate_bootstrap_components(components)
    errors: list[str] = []

    # SFTP `open(w)` 不會建立父目錄；路徑是本模組常數，沒有使用者輸入。
    await ssh_run_direct(server_cfg, f"mkdir -p '{BOOTSTRAP_REMOTE_DIR}'", 15.0)
    await write_file_direct(server_cfg, BOOTSTRAP_REMOTE_PATH, BOOTSTRAP_SCRIPT)
    result = await ssh_run_direct(
        server_cfg, build_bootstrap_command(ordered), command_timeout
    )

    component_states = parse_bootstrap_report(getattr(result, "stdout", "") or "")
    if component_states is None:
        errors.append("bootstrap 腳本沒有輸出可解析的 BOOTSTRAP_REPORT")
        component_states = {name: "unknown" for name in ordered}
    components_ok = set(component_states) == set(ordered) and all(
        component_states.get(name) in _COMPONENT_OK_STATES for name in ordered
    )
    if not components_ok and "bootstrap 腳本沒有輸出可解析的 BOOTSTRAP_REPORT" not in errors:
        failed = sorted(
            name
            for name in ordered
            if component_states.get(name) not in _COMPONENT_OK_STATES
        )
        if failed:
            errors.append(f"元件未就緒：{', '.join(failed)}（missing 的系統工具需操作者以 root 安裝）")

    capabilities: dict[str, Optional[str]] = {}
    checks = dict(CAPABILITY_CHECK_COMMANDS)
    if gpu:
        checks["nvidia-smi"] = CAPABILITY_CHECK_GPU_COMMAND
    for tool, command in checks.items():
        try:
            check = await ssh_run_direct(server_cfg, command, 15.0)
            capabilities[tool] = (getattr(check, "stdout", "") or "").strip() or None
        except Exception as exc:  # noqa: BLE001 - 單項檢查失敗記錄後繼續，彙總 fail-closed
            capabilities[tool] = None
            errors.append(f"capability check {tool} 失敗：{exc}")

    caps_ok, missing = evaluate_capabilities(capabilities, gpu=gpu)
    if missing:
        errors.append(f"capability check 缺項：{', '.join(missing)}")

    return {
        "script_version": BOOTSTRAP_SCRIPT_VERSION,
        "script_sha256": bootstrap_script_sha256(),
        "components": component_states,
        "capabilities": capabilities,
        "passed": bool(components_ok and caps_ok),
        "errors": errors,
    }
