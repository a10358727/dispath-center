"""政策驅動的放置候選評估(Goal 2 Slice 4,
docs/GOAL_2_AUTOMATED_DISPATCH_PLAN.md,DG-1 核准見 docs/DECISIONS.md
2026-07-18)。

純函式模組:只讀已經持久化的政策/伺服器觀測狀態,不做任何 SSH、不建立
approval、不寫資料庫。這裡的輸出只是「候選」——由呼叫端
(`app.main.AppState.auto_placement_loop()`)逐一丟給
`app.approvals.request_auto_placement_approval()` 建立 **pending** approval,
人核准了才真的建 job(鐵律第 2 條)。

不 import app.main(避免循環依賴,也讓這個模組能獨立單元測試,不需要起
FastAPI app);使用既有的 `app.monitor.is_idle()` 純函式做閒置判定,完全
不修改它。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from app.config import ServerConfig
from app.db import DispatchPolicy
from app.monitor import ServerState, is_idle


@dataclass(frozen=True)
class PlacementCandidate:
    """一個「政策 x 伺服器」的放置候選(尚未建立任何 approval)。"""

    policy_id: str
    policy_name: str
    policy_revision: int
    project_id: str
    project_name: str
    server_name: str


def _policy_is_within_validity(policy: DispatchPolicy, now_iso: str) -> bool:
    """`valid_until` 為 None 代表沒有期限;有值時必須晚於 `now_iso`。時間戳
    格式異常(理論上不會發生,`valid_until` 一律經
    `app.approvals._normalize_future_expiry()` 驗證才寫入)時保守判定為
    「已過期」,不猜測。"""

    if policy.valid_until is None:
        return True
    try:
        valid_until = datetime.fromisoformat(policy.valid_until)
        now = datetime.fromisoformat(now_iso)
    except (ValueError, TypeError):
        return False
    return valid_until > now


def evaluate_placement_candidates(
    *,
    policies: list[DispatchPolicy],
    server_states: dict[str, ServerState],
    server_configs: dict[str, ServerConfig],
    has_running_job_by_server: dict[str, bool],
    dataset_cached_lookup: Callable[[str, str, str], bool],
    project_dataset_lookup: Callable[[str], Optional[tuple[str, str]]],
    now_iso: str,
) -> list[PlacementCandidate]:
    """對每個 approved、未過期的政策,找出 `allowed_servers` 中符合放置
    條件的伺服器,回傳候選清單(每個 (policy, server) 組合這次呼叫最多
    出現一次)。

    條件依序:
    1. 政策必須是 `status == "approved"` 且 `valid_until` 未過期。
    2. 伺服器必須在政策的 `allowed_servers` 交集「已設定且 enabled」的
       伺服器。
    3. `policy.require_tag`(若有設定)必須出現在該伺服器 `ServerConfig.tags`
       中。
    4. `monitor.is_idle()`(既有純函式,完全不修改)判定為閒置——需要
       `has_running_job_by_server` 提供「這台機器目前有沒有本系統派發的
       running job」,由呼叫端(loop)從既有 `jobs` 表查出,這個模組本身
       不碰資料庫。
    5. `policy.dataset_required` 為真時:專案必須有已註冊的資料集綁定
       (`project_dataset_lookup(project_id)` 回傳 `(name, version)`,沒有
       綁定回傳 `None` 直接排除這個政策的所有伺服器——不會退化成「隨便選
       一個機器都算數」),且 `dataset_cached_lookup(server, name, version)`
       為真(資料集已經同步到這台機器)。

    純函式、決定性輸出:依 `(policy_name, server_name)` 排序,同一組輸入
    永遠得到同一份結果,方便測試與之後的稽核回放。

    `project_dataset_lookup` 是這個模組相對於原計畫文字描述的必要延伸——
    `dataset_cached_lookup` 只接受 (server, dataset_name, dataset_version)
    三個參數,而「這個專案有沒有註冊資料集」跟「已註冊的資料集有沒有同步
    到這台機器」是兩個獨立要檢查的條件,純函式本身不能反查
    `projects.dataset_name`/`dataset_version`(那需要 DB 存取),因此由呼叫
    端把這個投影也一併注入。
    """

    candidates: list[PlacementCandidate] = []
    for policy in policies:
        if policy.status != "approved":
            continue
        if not _policy_is_within_validity(policy, now_iso):
            continue

        dataset_binding: Optional[tuple[str, str]] = None
        if policy.dataset_required:
            dataset_binding = project_dataset_lookup(policy.project_id)
            if dataset_binding is None:
                continue

        for server_name in policy.allowed_servers:
            server_cfg = server_configs.get(server_name)
            if server_cfg is None or not server_cfg.enabled:
                continue
            if policy.require_tag and policy.require_tag not in server_cfg.tags:
                continue
            state = server_states.get(server_name)
            if state is None:
                continue

            idle = is_idle(
                online=state.online,
                has_running_job=has_running_job_by_server.get(server_name, False),
                is_gpu_server=server_cfg.gpu,
                gpu_util_max=state.gpu_util_max,
                idle_gpu_util=server_cfg.idle_gpu_util,
                load1=state.load1,
                idle_load=server_cfg.idle_load,
            )
            if not idle:
                continue

            if policy.dataset_required:
                dataset_name, dataset_version = dataset_binding
                if not dataset_cached_lookup(server_name, dataset_name, dataset_version):
                    continue

            candidates.append(
                PlacementCandidate(
                    policy_id=policy.id,
                    policy_name=policy.name,
                    policy_revision=policy.revision,
                    project_id=policy.project_id,
                    project_name=policy.project_name,
                    server_name=server_name,
                )
            )

    return sorted(candidates, key=lambda c: (c.policy_name, c.server_name))
