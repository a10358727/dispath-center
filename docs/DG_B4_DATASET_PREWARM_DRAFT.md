# DG-B4 草案：新機 dataset 預熱提案（Goal 3 Phase B4，可選）

> Status: **已裁定並實作（2026-07-25）**。DG-B4 核准紀錄見
> `docs/DECISIONS.md` 2026-07-25 條目；實作與驗證見
> `docs/CURRENT_STATE.md` §0.22。本文件保留為設計理由的存檔。
>
> 實作與本草案有**一處偏離**（已在裁定中一併核准）：下方「磁碟空間」
> 原寫提案時呼叫 `check_disk_space()`；實作改為提案時用 monitor 已探測
> 到的 `ServerState.disk_avail_bytes` 做 fail-closed 預檢，提案這一輪
> 完全不 SSH，權威 df 檢查仍在派工當下由 `app/scheduler.py` 執行。

## 解決什麼

Phase B（B1–B3）讓一台空機器可以透過 bootstrap+server_add 開通，但開通後
它的 `dataset_cache` 是空的——第一個真的需要某資料集的訓練任務，仍然要
等使用者手動建 sync 任務或改派其他機器。B4 想讓「常用資料集」在機器一
開通後就自動被**提案**（不是自動執行）預先同步過去。

## 提案設計

**觸發時機**：新增一個週期迴圈 `dataset_prewarm_loop()`（結構比照既有
`AppState.auto_placement_loop()`，`app/main.py:1075`），預設週期
`DATASET_PREWARM_INTERVAL_SEC=300`（比照 `AUTO_PLACEMENT_INTERVAL_SEC`）。
每輪：

1. 找出 `enabled=true` 且目前 `dataset_cache` 為空的機器（`db.list_dataset_cache(server=name)`
   回傳空列表）——只鎖定「看起來剛開通、還沒東西」的機器，不會對已經有
   資料的機器重複提案。
2. 對每個候選機器，用 `db.list_dataset_cache(server=None)` 算出「這個資料集
   目前被幾台其他 enabled 機器快取」，挑**被快取機器數最多**的那個資料集
   （data gravity 訊號：越多機器已經用得到，新機器越可能也需要）；同分時
   以 `Dataset.size_bytes` 較小者優先（越快傳完、越低風險）。
3. 用既有 `app.datasets.check_disk_space()` 確認目的地機器空間足夠——不夠
   直接跳過本輪，不留 pending、不重試到下一輪自然重新評估。
4. 每輪每台機器**最多提一個** dataset 的 sync 提案（不是一次提滿所有可能
   有用的資料集），限制單一 approval 的影響範圍，也讓使用者一次只需要
   看一張卡片。

**新 approval kind**：`dataset_prewarm`（`VALID_APPROVAL_KINDS` 新增第 32
種）。Payload：`{server, dataset, version, size_bytes, cached_on_count}`。
核准後走**既有** `app.datasets.build_sync_script()` / sync job 派發路徑
（跟訓練任務觸發的 sync 任務走同一條程式碼，不重新發明），**不**進
`enqueue|stop` 自動核准白名單——一律要人工點一次。

**煞車（比照 auto_placement 三件套）**：
- `DATASET_PREWARM_V1_ENABLED`（預設 `false`）：總開關，關閉時迴圈整輪
  是 no-op。
- `DATASET_PREWARM_KILL_SWITCH`（預設 `true` = 關）：跟
  `AUTO_PLACEMENT_KILL_SWITCH` 同款雙重保險——兩個旗標都要撥開才會真的
  提案。
- `DATASET_PREWARM_COOLDOWN_SEC`（預設 `3600`）：同一個 `(server, dataset,
  version)` 組合被使用者拒絕或核准後，冷卻時間內不重複提案；同一組合
  同時只允許一筆 pending（比照 `auto_placement` 的去重邏輯）。
- 每輪每台機器最多 1 筆新提案（見上）；`dataset_prewarm` approval 待核准
  期間，該機器不會再收到第二筆不同資料集的提案。

**Verification**：FakeSSH/db fixture 測試涵蓋——空 cache 機器才觸發、
非空 cache 機器不觸發、data-gravity 排序、同分 size 排序、磁碟不足跳過、
冷卻期內不重複提案、kill switch 開時整輪 no-op、approval 核准後走既有
sync 路徑產生跟訓練任務觸發的 sync job 完全一致的指令字串（golden test，
比照 C1 的驗證方式）。

## 裁定點

- [x] **核准以上設計為 DG-B4，開始實作**（預設 flags 全關，跟 Phase B/A1/D-1
  同樣的「先落地、後啟用」模式）— 2026-07-25 裁定
- [ ] 核准設計但要求修改：___________________________
- [ ] 不做 B4，維持人工建 sync 任務

裁定紀錄見 `docs/DECISIONS.md` 2026-07-25「DG-B4」條目。
