"""Goal 3 Phase A A1（docs/GOAL_3_FUTURE_WORK_PLAN.md）：Codex Runner 沙箱
**唯讀 preflight** ——只檢查、不啟用、不改任何遠端狀態。

背景（docs/DECISIONS.md D2，2026-07-16）：post-agent Git finalization 沙箱
需要三個硬前提，且 2026-07-15 的稽核已確認**開發 shell 無法替代真實 Runner
服務帳號**的檢查：

1. cgroup v2 可寫委派（systemd user scope 或等效）——聚合 CPU/RAM/process
   上限的強制機制；
2. ext4 project quota（或等效）——50 GiB task-disk 的強制機制；
3. bubblewrap 可建立 no-network namespace——finalization 的網路隔離。

這個模組提供純函式（腳本組裝＋輸出解析，直接可測），實際執行走既有
`ssh_run` 介面。所有檢查都是唯讀：`test`/`cat`/`grep`/`stat`/`id`，唯一的
「執行」是 `bwrap ... true`（在丟棄式 namespace 裡跑 `true`，無任何寫入
副作用——不建這個 namespace 就無法證明能建）。解析 fail-closed：缺 section
、輸出不可解析一律 `unknown`，`ready` 只有在**每一項都明確通過**時才為
True——unknown 不是通過（同 `is_idle()` 的慣例）。

DG-A 閘門：`ready=True` 只代表「硬前提存在」；資源數字與啟用條件仍需
操作者在真實 Runner 對照 preflight 結果後另行裁定（G2，見核准計畫檔）。
"""

from __future__ import annotations

import re
from typing import Optional

#: 每個檢查一個 section，marker 慣例同 `app/monitor.py` 的探測輸出。
#: 全部唯讀（見模組 docstring 對 `bwrap ... true` 的說明）。
SANDBOX_PREFLIGHT_SCRIPT = r"""
echo '---BWRAP---'
command -v bwrap >/dev/null 2>&1 && bwrap --version 2>/dev/null || echo 'absent'
echo '---BWRAP-NETNS---'
if command -v bwrap >/dev/null 2>&1; then
  bwrap --ro-bind / / --unshare-net --die-with-parent true >/dev/null 2>&1 \
    && echo 'ok' || echo 'failed'
else
  echo 'absent'
fi
echo '---CGROUP-CONTROLLERS---'
cat /sys/fs/cgroup/cgroup.controllers 2>/dev/null || echo 'absent'
echo '---CGROUP-USER-DELEGATION---'
UID_NOW=$(id -u)
USER_CG="/sys/fs/cgroup/user.slice/user-${UID_NOW}.slice/user@${UID_NOW}.service"
if [ -d "$USER_CG" ] && [ -w "$USER_CG/cgroup.procs" ]; then
  cat "$USER_CG/cgroup.controllers" 2>/dev/null || echo 'unreadable'
else
  echo 'absent'
fi
echo '---SYSTEMD-USER-BUS---'
[ -S "/run/user/$(id -u)/bus" ] && echo 'ok' || echo 'absent'
echo '---PRJQUOTA---'
grep -E '(^|[ ,])prjquota([ ,]|$)' /proc/mounts >/dev/null 2>&1 && echo 'ok' || echo 'absent'
echo '---END---'
"""

#: finalization 沙箱要求 user-delegated subtree 至少有這些 controller。
REQUIRED_DELEGATED_CONTROLLERS = ("cpu", "memory", "pids")

_SECTION_RE = re.compile(r"^---([A-Z-]+)---$", re.MULTILINE)


def build_sandbox_preflight_script() -> str:
    """純函式：回傳固定 preflight 腳本（沒有任何參數/使用者輸入）。"""

    return SANDBOX_PREFLIGHT_SCRIPT


def _split_sections(stdout: str) -> Optional[dict[str, str]]:
    """把 `---MARKER---` 輸出切成 {marker: body}；沒有 END marker → None
    （輸出被截斷，整份不可信）。"""

    if not stdout:
        return None
    matches = list(_SECTION_RE.finditer(stdout))
    if not matches or matches[-1].group(1) != "END":
        return None
    sections: dict[str, str] = {}
    for current, following in zip(matches, matches[1:]):
        sections[current.group(1)] = stdout[current.end() : following.start()].strip()
    return sections


def parse_sandbox_preflight_output(stdout: str) -> dict:
    """解析 preflight 輸出成結構化報告（純函式、fail-closed）。

    回傳 shape::

        {"checks": {name: {"status": "pass"|"fail"|"unknown", "detail": str}},
         "ready": bool}

    `ready` 只有在每一項 status 都是 "pass" 時才 True；任何 unknown 都
    不是通過。"""

    def check(status: str, detail: str) -> dict:
        return {"status": status, "detail": detail}

    sections = _split_sections(stdout or "")
    if sections is None:
        names = (
            "bwrap", "bwrap_no_network", "cgroup_v2",
            "cgroup_user_delegation", "systemd_user_bus", "project_quota",
        )
        return {
            "checks": {name: check("unknown", "preflight 輸出缺失或被截斷") for name in names},
            "ready": False,
        }

    checks: dict[str, dict] = {}

    bwrap = sections.get("BWRAP", "")
    if bwrap and bwrap != "absent":
        checks["bwrap"] = check("pass", bwrap)
    elif bwrap == "absent":
        checks["bwrap"] = check("fail", "bubblewrap 未安裝")
    else:
        checks["bwrap"] = check("unknown", "無輸出")

    netns = sections.get("BWRAP-NETNS", "")
    if netns == "ok":
        checks["bwrap_no_network"] = check("pass", "可建立 no-network namespace")
    elif netns in ("failed", "absent"):
        checks["bwrap_no_network"] = check("fail", "無法建立 no-network namespace")
    else:
        checks["bwrap_no_network"] = check("unknown", "無輸出")

    controllers = sections.get("CGROUP-CONTROLLERS", "")
    if controllers and controllers != "absent":
        checks["cgroup_v2"] = check("pass", controllers)
    elif controllers == "absent":
        checks["cgroup_v2"] = check("fail", "沒有 cgroup v2 unified hierarchy")
    else:
        checks["cgroup_v2"] = check("unknown", "無輸出")

    delegation = sections.get("CGROUP-USER-DELEGATION", "")
    if delegation in ("absent", "unreadable", ""):
        checks["cgroup_user_delegation"] = check(
            "fail" if delegation else "unknown",
            "user scope 不存在或 cgroup.procs 不可寫" if delegation else "無輸出",
        )
    else:
        delegated = set(delegation.split())
        missing = [c for c in REQUIRED_DELEGATED_CONTROLLERS if c not in delegated]
        if missing:
            checks["cgroup_user_delegation"] = check(
                "fail", f"可寫但缺 controller：{', '.join(missing)}（委派不完整）"
            )
        else:
            checks["cgroup_user_delegation"] = check("pass", delegation)

    bus = sections.get("SYSTEMD-USER-BUS", "")
    if bus == "ok":
        checks["systemd_user_bus"] = check("pass", "user bus socket 存在")
    elif bus == "absent":
        checks["systemd_user_bus"] = check("fail", "沒有 systemd user bus")
    else:
        checks["systemd_user_bus"] = check("unknown", "無輸出")

    quota = sections.get("PRJQUOTA", "")
    if quota == "ok":
        checks["project_quota"] = check("pass", "掛載含 prjquota 選項")
    elif quota == "absent":
        checks["project_quota"] = check("fail", "沒有任何 prjquota 掛載")
    else:
        checks["project_quota"] = check("unknown", "無輸出")

    ready = all(item["status"] == "pass" for item in checks.values())
    return {"checks": checks, "ready": ready}
