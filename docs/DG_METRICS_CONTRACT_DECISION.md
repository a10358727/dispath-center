# DG-METRICS-CONTRACT v1 — metrics-v1 檔案契約決策閘

> Status: **awaiting ruling**
> 本文件是 review packet，不是核准紀錄。裁定後記入 `docs/DECISIONS.md`。
> 來源：`docs/product/FULL_PLATFORM_SECOND_PASS_PLAN.md` Part E §E-3 草案
> 骨架（Stage 2）；DG-PERSONAL-PILOT-v1 D3 曾明文延後並保留
> `metrics-v1` 契約名（`docs/DECISIONS.md:1268-1274`）。

## 1. 問題

M3 Experiment Dashboard 與 M4 限額優化迴圈都需要可比較的結構化
metrics，但現況**不存在任何 metrics 解析**：

- results 收集只有 job-finish hook 的 rsync 拉取
  （`app/jobfinish.py:773` → `app/results.py:92`），落地
  `{local_home}/results/{job_id}/`；收不收得到永不影響 job 狀態
  （`app/jobfinish.py:846-899`，INV-SSH-6/7 姿態）。
- 唯一被解析的檔案是 coding run 的 `result.json`
  （`app/jobfinish.py:502-513`）；`metrics.json` 只有前端 raw 文字預覽
  （`static/ui.js:4667`），無 schema、無入庫、無 `run_metrics` 表。
- 分析規則（`result-analysis.md`）要求結論附證據、missing = unknown、
  收集失敗與執行狀態分離——沒有結構化 metrics 就無法支撐。

## 2. 契約內容（A 案正文）

**檔案契約**：workload 自行寫 `results/{job_id}/metrics.json`（經既有
rsync 一併收回，**零新遠端指令**，INV-SSH-4 不動）：

- 單一扁平 JSON object；≤64 KiB；≤256 keys。
- key 字元集封閉：`[a-z0-9_.]`，長度 1–128。
- 值只允許：JSON integer、bool、bounded string（≤4096 bytes）、
  canonical decimal string（沿用 `canonical_decimal()` 規則，
  `app/project_bootstrap.py:411`；**拒絕 JSON float**，與
  `canonical_json()` 的 float 禁令一致，`app/execution_contract.py:23`）。

**解析點**：job-finish hook 在 `pull_job_results` 成功後、Server A 本地
**純函式**解析（新模組，零 I/O 副作用入函式）；既有 engineering-task
recovery 路徑（`app/jobfinish.py:973`）同樣收斂。解析失敗/超限/缺檔
永不影響 job 狀態機、reconciliation、scheduling——哨兵 exit_code 仍是
唯一終態來源（INV-SSH-6 明文不動）。

**儲存**：additive migration v13（依 `app/migrations.py` 既有
checksum-pinned 模式）：

- `run_metrics(job_id, key, value_type, value_text, recorded_at)`，
  `UNIQUE(job_id, key)`；值一律以 canonical 文字形式入庫。
- `run_metrics_collection(job_id UNIQUE, status, reason, source_sha256,
  collected_at)`，`status ∈ collected|missing|invalid|oversize`；
  reason 為 bounded 字串。**永不寫在 `jobs` 上**（`jobs` 維持零
  collection 欄位的現況）。

**語意**（承 `result-analysis.md`）：missing = unknown（不是 0、不是
失敗）；invalid 必附 reason；重複解析冪等（同 job 重收斂以
`source_sha256` 判斷、整批替換）。

**讀取面（Stage 2 範圍內最小投影）**：唯讀 `GET /jobs/{job_id}/metrics`
（沿用既有 results API 的 auth 慣例，`app/main.py:10804` 一帶）+
Product Run projection 增加 `metrics_status`／metrics 摘要
（`app/product_run_store.py:303` 的 `collection_state` 同構）。
Dashboard/Compare UI 本身屬 M3，另案。

**旗標**：`METRICS_V1_ENABLED=false` 預設關閉（`app/settings/features.py`
`_flag` 模式）；off 時零解析、零新表寫入、endpoint 404/預設拒絕，
現行為零改變。

**明文不做（已否決替代案，承 §E-3）**：log scraping（違反
grounded-evidence）；worker push endpoint（顛倒單向信任邊界）；
metrics 影響任務終態或排程；新 approval kind（解析是收集後的本地
唯讀衍生 + 系統內部入庫，不是使用者 mutating entry point）；
歷史 results 目錄 backfill（延後，另以唯讀工具處理）。

## 3. 選項

- **A（推薦）— 完整 metrics-v1 契約如 §2**：解析 + 入庫 + 唯讀投影，
  default-off。M3/M4 的資料地基一次落地，證據可追溯（status 四態 +
  sha256）。
- **B — parse-on-read**：不入庫、不 migration，results API 查詢時
  即時解析。少一張表，但無 durable evidence、無 status 四態紀錄、
  compare 需重複讀檔，且 M4 迴圈仍得補回入庫——只是把 A 拆兩次做。
  不推薦。
- **C — 再延後**：M3/M4 持續無地基；`§15`「部分已實作」的現況誤述
  繼續存在。不推薦。

## 4. A 案實作範圍（裁定後交 sonnet-coder）

純函式 `parse_metrics_v1(raw_bytes) -> ParsedMetrics|MetricsInvalid`
（大小/keys/字元集/值型別全部 fail-closed）；migration v13 雙軌測試
（`tests/test_migrations.py` 釘 13）；job-finish hook 在既有
`result_pulled` 之後掛解析（失敗只記 audit，不動 job）；audit 進既有
family（新 action 依 `tests/test_audit_actor.py` 計數閘流程更新）；
唯讀 endpoint + auth 覆蓋（`test_authorization_coverage`、
`test_openapi_contract` 計數閘依文件化流程更新）；features.py 新旗標
（`test_typed_settings` 唯一性閘）；測試含 collected/missing/invalid/
oversize 四態、float 拒絕、冪等重收斂、flag off 零行為變化、
收集失敗與解析失敗互不污染 job 狀態。全程 fake，不碰真 worker。

**共同邊界**：不改 invariant/釘住測試語意；零新 approval kind；
`TOOLS` 表不動；auto-approve 白名單不動；INV-SSH-4/6/7 一字不動。
**BLOCKED 條件**：發現需要新遠端指令、需要動 job 狀態機、或
migration 無法 additive。

## 5. 裁定模板

```text
DG-METRICS-CONTRACT：A 核准 / B / C（延後）
```
