"""Goal 3 Phase B4：新機 dataset 預熱的**純函式**候選評估。

DG-B4 核准見 `docs/DECISIONS.md` 2026-07-25，設計草案見
`docs/DG_B4_DATASET_PREWARM_DRAFT.md`。

解決什麼：Phase B（B1–B3）讓空機器可以 bootstrap + `server_add` 開通，但
開通後它的 `dataset_cache` 是空的——第一個真的需要某資料集的訓練任務，
仍然要等使用者手動建 sync 任務或改派其他機器。這裡讓「其他機器已經在用
的資料集」在新機開通後被自動**提案**預先同步過去。

邊界（跟 `app/auto_placement.py` 同一套紀律）：

- 這個模組**只算候選**，不建 approval、不建 job、不碰 SSH、不寫 DB。
  副作用全部在 `app/approvals.py:request_dataset_prewarm_approval()` 與
  `app/main.py:AppState._dataset_prewarm_tick()`。
- 提案 ≠ 執行。核准後才走既有 `build_sync_script()` / `type="sync"` job
  路徑（`app/approvals.py` 的 `dataset_prewarm` 分支），指令組裝與手動
  派工附帶的 sync 任務逐位元一致。
- `dataset_prewarm` 永遠不進 `maybe_auto_approve()` 的 `enqueue`/`stop`
  白名單——一律要人工點一次。

政策（DG-B4 核准內容）：

1. **觸發對象**：`enabled=true` 且 `dataset_cache` 為空的機器。只鎖定
   「看起來剛開通、還沒東西」的機器；已經有任何快取的機器一律不提案，
   避免對長期運作中的機器持續灌提案。
2. **資料集選取**：data gravity——挑「目前被最多其他 enabled 機器快取」
   的資料集（越多機器已經用得到，新機器越可能也需要）。同分時
   `size_bytes` 較小者優先（越快傳完、越低風險）；再同分時以
   (name, version) 字典序決定，確保**同輸入同輸出**（比照 `pick_job`／
   `pick_codex_runner` 的確定性要求）。
3. **磁碟空間**：用 monitor 已經探測到的 `disk_avail_bytes` 做提案時的
   便宜預檢，fail-closed——`None`（離線／df 失敗／還沒探測過）視為不足，
   直接跳過本輪，不留 pending。這**不是**權威檢查：真正的 df 檢查仍然
   在派工當下由 `app/scheduler.py` 既有的 sync 前 `check_disk_space()`
   執行（同一個 `SPACE_SAFETY_FACTOR`），本模組只是避免提出一眼就知道
   放不下的提案。
4. **每輪每台機器最多一個提案**：限制單一 approval 的影響範圍，也讓
   使用者一次只需要看一張卡片。冷卻與去重在
   `request_dataset_prewarm_approval()` 內部判斷（比照
   `request_auto_placement_approval()`），不在這裡重複。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from app.datasets import SPACE_SAFETY_FACTOR


@dataclass(frozen=True)
class PrewarmCandidate:
    """一台目標機器 + 一個建議預熱的資料集版本。"""

    server_name: str
    dataset_name: str
    dataset_version: str
    size_bytes: int
    #: 目前有幾台**其他** enabled 機器已經快取這個 (name, version)——
    #: data gravity 的證據，會原樣放進 approval payload 供人判斷。
    cached_on_count: int


def evaluate_prewarm_candidates(
    *,
    enabled_server_names: Iterable[str],
    cache_entries: Iterable,
    datasets_by_key: Mapping[tuple[str, str], object],
    disk_avail_by_server: Mapping[str, Optional[int]],
    space_safety_factor: float = SPACE_SAFETY_FACTOR,
) -> list[PrewarmCandidate]:
    """算出這一輪要對哪些機器提案預熱哪個資料集。純函式，無副作用。

    參數刻意全部是已經讀好的現況快照（不接 `Database`），讓政策可以被
    直接測試，同 `app.auto_placement.evaluate_placement_candidates()` 的
    既有慣例。

    - ``enabled_server_names``：目前 `enabled=true` 的機器名。
    - ``cache_entries``：全域 `dataset_cache`（`db.list_dataset_cache()`
      不帶 server 參數的回傳值）。
    - ``datasets_by_key``：`(name, version) -> Dataset`，用來取
      `size_bytes`。查不到的 (name, version) 直接跳過——快取表指向一個
      已經不存在的資料集版本時不提案，不臆造大小。
    - ``disk_avail_by_server``：monitor 最近一次探測到的可用空間；缺值或
      `None` 一律視為不足（fail-closed）。

    回傳依 `server_name` 排序的候選列表（確定性）。
    """
    enabled = sorted(set(enabled_server_names))
    enabled_set = set(enabled)

    #: (dataset, version) -> 已快取的 enabled 機器集合。非 enabled 機器的
    #: 快取列不列入 data gravity 訊號——它們不會被排程挑中，不構成
    #: 「大家都在用」的證據。
    cached_servers_by_key: dict[tuple[str, str], set[str]] = {}
    #: 每台機器目前快取了什麼（用來判斷「cache 是否為空」）。
    cache_by_server: dict[str, set[tuple[str, str]]] = {}
    for entry in cache_entries:
        key = (entry.dataset, entry.version)
        cache_by_server.setdefault(entry.server, set()).add(key)
        if entry.server in enabled_set:
            cached_servers_by_key.setdefault(key, set()).add(entry.server)

    candidates: list[PrewarmCandidate] = []
    for server_name in enabled:
        # 觸發條件：這台機器完全沒有快取任何東西。
        if cache_by_server.get(server_name):
            continue

        avail = disk_avail_by_server.get(server_name)
        if avail is None:
            # fail-closed：探測不到空間就不提案（離線／df 失敗／尚未探測）。
            continue

        best: Optional[PrewarmCandidate] = None
        for key, servers in cached_servers_by_key.items():
            others = servers - {server_name}
            if not others:
                continue
            dataset = datasets_by_key.get(key)
            if dataset is None:
                continue
            size_bytes = int(getattr(dataset, "size_bytes", 0) or 0)
            if avail < int(size_bytes * space_safety_factor):
                continue
            candidate = PrewarmCandidate(
                server_name=server_name,
                dataset_name=key[0],
                dataset_version=key[1],
                size_bytes=size_bytes,
                cached_on_count=len(others),
            )
            if best is None or _rank(candidate) < _rank(best):
                best = candidate
        if best is not None:
            candidates.append(best)
    return candidates


def _rank(candidate: PrewarmCandidate) -> tuple:
    """排序鍵：被快取機器數多者優先 → size 小者優先 → (name, version)
    字典序。最後一段保證同輸入同輸出（dict 迭代順序不影響結果）。"""
    return (
        -candidate.cached_on_count,
        candidate.size_bytes,
        candidate.dataset_name,
        candidate.dataset_version,
    )
