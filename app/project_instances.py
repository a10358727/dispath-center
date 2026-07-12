"""Instance reconciliation（PLAN.md 2026-07-11 版 §14 切片 2、§6.3）。

定期以唯讀 SSH 探測把每個已登記 `project_instances` 的現況收斂成
`state`（值域見 `app.db.VALID_INSTANCE_STATES`）：

- `available`：路徑存在、乾淨,且（有 hub 時）commit 等於 hub HEAD。
- `missing`：路徑不存在了（列不刪,人來決定,INV-PROJECT-4 草案）。
- `dirty`：working tree 有未提交修改。
- `diverged`：乾淨但 commit 不等於中央 hub HEAD。
- `unknown`：機器離線/探測失敗/尚未 reconcile——**連不上不代表 missing**
  （INV-SSH-7 的同一精神）。

安全邊界（違反＝實作錯誤）:

- **只探測 DB 已登記的 `(server, path)`**,不接受任意路徑（同
  `app.activity.probe_instance()` 的邊界）。
- **全程唯讀**:只組 `[ -d ]`／`git branch|rev-parse|status --porcelain`
  這類封閉唯讀指令（INV-SSH-4）,不寫遠端、不執行使用者自訂指令。
- 探測結果先落 DB（`update_instance_reconcile()`）——沒有遠端副作用,
  不涉及 INV-STATE-2 的先後問題。
- 呼叫端（`app/main.py` 的 `project_instance_reconcile_loop()`）是獨立
  背景迴圈,不佔排程輪（INV-STATE-6）;list/detail API 讀的都是本模組
  上一輪落地的快照,不即時 SSH（INV-PROJECT-3 草案）。

模式同 `app/inventory.py`／`app/activity.py`:純函式（build command／
parse／derive）與 async 主函式分離,測試注入假 `ssh_run`。
"""

from __future__ import annotations

import shlex
from typing import Any, Awaitable, Callable, Optional

from app.db import Database, ProjectInstance

#: 單一 instance 探測的 SSH 逾時（秒）——比照 `app.activity.probe_instance()`
#: 的 find 逾時等級;`git status --porcelain` 在大 repo 上可能要掃 working
#: tree,給到 20 秒。
PROBE_TIMEOUT_SEC = 20


def build_instance_probe_command(path: str) -> str:
    """組出單一 instance 的唯讀探測指令:目錄存在性＋branch/commit/dirty
    一次拿回（減少 SSH round trip;比 `app.inventory.build_git_info_commands()`
    的三連發多了存在性與 dirty,輸出用 `KEY:value` 行,同 inventory 的
    marker 慣例）。非 git 目錄時 BRANCH/COMMIT 是空值、DIRTY:0,照樣解析,
    不讓整個探測失敗。"""
    p = shlex.quote(path)
    return (
        f"if [ -d {p} ]; then "
        f"echo EXISTS:1; "
        f'echo "BRANCH:$(git -C {p} branch --show-current 2>/dev/null)"; '
        f'echo "COMMIT:$(git -C {p} rev-parse HEAD 2>/dev/null)"; '
        f'if [ -n "$(git -C {p} status --porcelain 2>/dev/null | head -c 1)" ]; '
        f"then echo DIRTY:1; else echo DIRTY:0; fi; "
        f"else echo EXISTS:0; fi"
    )


def parse_instance_probe_output(text: str) -> Optional[dict]:
    """解析 `build_instance_probe_command()` 的輸出。回傳
    `{"exists": bool, "branch": str|None, "commit": str|None, "dirty": bool}`;
    完全沒有 `EXISTS:` 標記（指令沒跑成、輸出被截斷）回 `None`,呼叫端
    視同探測失敗（state=unknown）,不腦補。"""
    exists: Optional[bool] = None
    branch: Optional[str] = None
    commit: Optional[str] = None
    dirty = False
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("EXISTS:"):
            exists = line[len("EXISTS:"):] == "1"
        elif line.startswith("BRANCH:"):
            branch = line[len("BRANCH:"):].strip() or None
        elif line.startswith("COMMIT:"):
            commit = line[len("COMMIT:"):].strip() or None
        elif line.startswith("DIRTY:"):
            dirty = line[len("DIRTY:"):].strip() == "1"
    if exists is None:
        return None
    return {"exists": exists, "branch": branch, "commit": commit, "dirty": dirty}


def derive_instance_state(probe: Optional[dict], *, hub_head: Optional[str]) -> str:
    """純函式:探測結果 → state。判定順序固定（missing > dirty > diverged >
    available）,可從輸入完整重現（INV-SCHED-1 草案的同一精神）:

    - 探測失敗（`probe is None`）→ `unknown`。
    - 路徑不存在 → `missing`。
    - working tree 髒 → `dirty`（優先於 diverged:先處理未提交修改才談
      版本差異）。
    - 有 hub HEAD、instance 有 commit,兩者不同 → `diverged`;instance
      根本不是 git repo（commit 為 None）時**不算 diverged**——那是
      「未 git 化」,由 git_init 流程處理,不是版本漂移。
    - 其餘 → `available`（含「專案沒有 hub」:沒有比較基準就不宣稱漂移）。
    """
    if probe is None:
        return "unknown"
    if not probe["exists"]:
        return "missing"
    if probe["dirty"]:
        return "dirty"
    if hub_head and probe.get("commit") and probe["commit"] != hub_head:
        return "diverged"
    return "available"


async def reconcile_all_instances(
    db: Database,
    ssh_run: Callable[..., Awaitable[Any]],
    is_server_online: Callable[[str], bool],
    hub_head_for: Callable[[str], Awaitable[Optional[str]]],
) -> list[dict]:
    """對 DB 全部 `project_instances` 做一輪唯讀 reconcile,回傳「state 有
    變化」的清單（供呼叫端寫稽核）。

    - 離線機器的 instance → `unknown`（不發 SSH,git 欄位與 last_seen
      不動——最後一次真的看到它的時間不因離線而改變）。
    - `hub_head_for(project_name)` 由呼叫端注入（`app/main.py` 用
      `app.hub.get_project_hub_info()` 包一層並在單輪內快取）;傳回 None
      ＝沒有 hub,不做 diverged 判定。
    - 單一 instance 失敗不影響其他 instance（同 `probe_instance()` 慣例）。
    """
    changes: list[dict] = []
    hub_head_cache: dict[str, Optional[str]] = {}

    for inst in db.list_all_project_instances():
        if not is_server_online(inst.server):
            new_state = "unknown"
            probe = None
        else:
            cmd = build_instance_probe_command(inst.path)
            try:
                result = await ssh_run(inst.server, cmd, PROBE_TIMEOUT_SEC)
                probe = parse_instance_probe_output(result.stdout or "")
            except Exception:  # noqa: BLE001 - 單台失敗＝unknown,不讓整輪掛掉
                probe = None
            if inst.project_name not in hub_head_cache:
                hub_head_cache[inst.project_name] = await hub_head_for(inst.project_name)
            new_state = derive_instance_state(
                probe, hub_head=hub_head_cache[inst.project_name]
            )

        if probe is not None and probe["exists"]:
            # 真的看到了:git 欄位與 last_seen 一併更新。
            db.update_instance_reconcile(
                inst.id,
                state=new_state,
                git_branch=probe["branch"],
                git_commit=probe["commit"],
                dirty=probe["dirty"],
                touch_last_seen=True,
            )
        else:
            # unknown/missing:只動 state,不改 git 快照、不推進 last_seen
            # ——那些欄位記錄的是「最後一次確實觀察到」的事實。
            db.update_instance_reconcile(inst.id, state=new_state)

        if new_state != inst.state:
            changes.append(
                {
                    "instance_id": inst.id,
                    "project": inst.project_name,
                    "server": inst.server,
                    "from": inst.state,
                    "to": new_state,
                }
            )
    return changes
