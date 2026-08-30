# DG-HARDWARE-EXECUTION v1 — 硬體工程軌執行契約（草稿）

> 狀態：**草稿，待使用者裁定**（2026-08-30 起草；使用者已核准「硬體軌可以開始」＝可以起草，
> 不等於核准本契約）。核准後摘要記入 `docs/DECISIONS.md`，本檔為權威細節（provenance）。
> 本草稿**不改任何 canonical invariant**；凡需要新 approval kind、新 preflight kind、新 probe
> 段落的地方，都是本裁定要具名核准的範圍。動工前的實作真相以程式碼與 `docs/CAPABILITY_LEDGER.md`
> 為準——目前**零實作**（程式碼裡沒有任何 fpga／mcu／bitstream／firmware／jtag／serial 概念）。

---

## 0. 問題（Problem）

定位把 FPGA synthesis／bitstream、MCU build／flash 與硬體驗證納入目標範圍
（`docs/PLATFORM_CHARTER.md` §1–§2）；憲章 §4.2 與 INV-PLANE-2 已固定邊界：**硬體動作屬 Compute
Plane 受治理執行，實體動作（flash／program／erase／power）永不從 agent workspace 發起、永不自動核准、
永不由 agent 工具直接觸發**。本裁定要回答的是「怎麼在現有執行模型上長出硬體軌」，共六題
（憲章 §7.3）：資源模型、工作類型、artifact、證據、排程、安全。

現有可承接的零件（實查）：

| 既有零件 | 位置 | 對硬體軌的意義 |
|---|---|---|
| `ServerConfig.tags`（自由字串、無語意）＋ `require_tag`／`required_server_tags` 資格過濾 | `app/config.py:57`、`app/scheduler.py:261`、`app/execution_plan_v2_store.py:730-800` | 唯一的「能力」槽；沒有裝置概念 |
| servers.yaml 修訂／發布協議（revision-pinned、digest、backup→原子寫→熱重載） | `app/server_config.py`、`app/server_publication.py` | 裝置宣告可搭同一條協議，ExecutionPlan 已釘 `server_config_revision` |
| monitor 封閉探測指令（nvidia-smi／loadavg／df／free／nproc，段落標記） | `app/monitor.py:146-171` | 裝置在場探測可加一段，仍是封閉指令集（INV-SSH-4） |
| `run-template-spec-v2`：`argv_template`（第一個 token 必為 literal 可執行檔）、`parameter_schema`、`resource_requirements`、`output_declarations`（file／directory＋path_pattern） | `app/project_bootstrap.py:388-700` | synthesis／build／program／test 都能寫成模板；`output_declarations` 是登記映像的天然入口 |
| `host-environment-v1`：`setup_command`、`required_server_tags`、`preflight_checks`（五種 kind，**執行面只接受 `server_tag_present`**） | `app/project_bootstrap.py:288-363`、`app/execution_plan_v2_store.py:506-518` | 工具鏈（vivado／openocd／esptool）屬 environment；`executable_present` 目前被拒 |
| ExecutionPlan v2：`backend: Literal["ssh"]`、`exclusive_worker` 永為 True、資源觀測 ≤60s | `app/execution_plan_v2.py:467-560`、`execution_plan_v2_store.py:594-661` | 一機一件與新鮮觀測都已存在 |
| `results/{job_id}/` rsync 回收 + `output_declarations` + metrics-v1 純函式解析 | `app/results.py:44-55`、`app/jobfinish.py:144-200`、`app/metrics_v1.py` | 建置產物與報告會**真的回到 Server A**（不是 metadata-only） |
| 三張 artifact 表（engineering／attempt／node）皆 metadata-only 或任務域專用 | `app/db.py:1076,1330`、`app/execution_attempt_schema.py:169` | 映像需要「Server A 持有位元組＋provenance」，現有表都不做這件事 |
| `is_dangerous()` 黑名單：`dd`／`mkfs`／`shutdown`／`reboot`／`userdel`／`rm -rf`／`> /dev/sd*` | `app/security.py:28-78` | 燒錄／擦除指令不在黑名單、也不該靠黑名單分辨 |
| Node attempt unit：`PrivateUsers=yes`、`ProtectSystem=strict`、無 `DeviceAllow` | `agent/isolation.py:85-106` | Node 後端目前碰不到 `/dev/*` |
| 核准 kinds：`execution_plan_v2` 為 transaction-only、high-risk、永不自動核准；`HIGH_RISK = VALID − {enqueue, stop}` | `app/db.py:285-427`、`app/authorization_shadow.py:91` | 任何新 kind 自動 high-risk；仍要在 `app/authorization.py:291-391` 分類 |

---

## 1. 建議契約（Recommended contract）

### H-1 資源模型：裝置是 worker 的附掛資源，宣告在 servers.yaml，revision-pinned

- `ServerConfig` 新增 `devices: list[DeviceSpec]`（預設空）。`DeviceSpec` 封閉欄位：
  `id`（`[A-Za-z0-9._-]{1,64}`，機器內唯一）、`kind`（封閉列舉：`fpga`、`mcu`、`programmer`、`power`）、
  `model`（描述字串 ≤64）、`serial`（≤128，選填）、`tags: list[str]`（同 server tags 規則）、
  `presence`（封閉列舉的在場探測方式，見下）、`power_control`（僅 `kind=power`：`usb_relay`｜`pdu_http`；
  指令由純函式依 kind 產生，永不接受自由文字）。
- **在場探測（presence）只能是封閉列舉**，每種對應一條純函式產生、只插值已驗證識別字的唯讀指令：
  `usb_vidpid:<vvvv:pppp>` → `lsusb -d vvvv:pppp`；`serial_by_id:<name>` → `test -e /dev/serial/by-id/<name>`；
  `path:<validated-path>` → `test -e <path>`。monitor 探測指令追加段落 `---DEVICES---`，逐裝置輸出
  `id present|absent`；解析成 `ServerState.devices`；未觀測＝`unknown`，不是 absent（INV-SSH-7 同構）。
- 宣告變更走既有 `server_update` 路徑（DG-INFRA-DIRECT-ACTIONS：驗證先行、備份、稽核、產生新
  `server_config_revision`；**預檢證據歸零**的規則同樣適用）；`validate_server_config()` 增加 devices 驗證。
- 執行面資格：ExecutionPlan／模板的 `resource_requirements.required_devices: list[DeviceRequirement]`
  （`kind` ＋ `tags`／`id` 精確匹配）必須同時滿足：(a) 目標 `server_config_revision` 宣告了該裝置；
  (b) 最近一次觀測（≤60s，沿 `_resource_observation`）為 present。錯誤碼 `target_device_missing`／
  `target_device_observation_unknown`／`target_device_absent`。
- **`executable_present` preflight 開放給執行面**（目前只收 `server_tag_present`）：以封閉唯讀指令
  `command -v <validated-name>` 產生證據，寫入 `environment_readiness`；這是本裁定明文核准的
  preflight resolver 擴張（`app/execution_plan_v2_store.py:506-518`）。其餘三種 kind 維持拒絕。

### H-2 工作類型：以 `action_class` 分級，實體動作獨立成 `hardware_action_v2` 核准 kind

- `run-template-spec-v2` 新增 `action_class`（封閉列舉，預設 `compute`）：
  `compute`（訓練／推論；現行全部模板）、`build`（synthesis／bitstream／firmware build——只產檔，無實體副作用）、
  `program`（flash／program／erase）、`power`（電源週期）、`hil_test`（對已燒錄裝置跑測試）。
- **`compute`／`build` 沿用 `execution_plan_v2`**：它們就是產生 artifact 的計算工作，不改 kind。
- **`program`／`power`／`hil_test` 走新 approval kind `hardware_action_v2`**（transaction-only、high-risk、
  永不自動核准；`maybe_auto_approve()` 白名單不動，INV-APPROVAL-4 不變）。核准 payload 釘：
  `execution_plan_v2` 等價的完整 spec ＋ `action_class` ＋ `device_id`（精確）＋ `image_sha256`
  （`program` 必填，見 H-3）＋ `power_sequence`（僅 `power`，封閉列舉 `off_on`／`reset`）。
  核准卡必須顯示：伺服器、裝置 id／型號／序號、映像 digest 與來源 build run、動作類別。
- 為什麼要新 kind 而不只是欄位：(1) 核准卡與稽核一眼可辨「這是會動到板子的事」；(2) authorization
  分類可獨立（建議 `project.operate` 提案、`project.admin` 決定；`app/authorization.py:291-391` 需分類）；
  (3) **結構性排除**：`experiment_create_v2` 的 matrix 拒絕 `action_class ∉ {compute, build}`
  （一次核准 N 個實體動作不在 v1）；DG-DEV-OPERATOR-DIRECT 的「底層」排除條款明文涵蓋
  `hardware_action_v2`（只有使用者本人能決定）；agent 的 `request_*` 工具可以建 `hardware_action_v2`
  待審卡（INV-LLM-1 上限），但永不決定、永不觸發。
- 實體動作在工作機的落地形狀不變：仍是 approved Job → SSH／SFTP／tmux／哨兵（INV-SSH-2/3/6），
  `cmd.sh` 由模板 argv 編譯（shell-free），電源控制指令由純函式依 `power_control.kind` 產生。

### H-3 Artifact：映像登記表 + Server A 內容定址保存

- 新增 additive 表 `hardware_images`：`id`、`project_id`、`project_version_id`、`build_plan_id`
  （產生它的 `execution_plans.id`）、`job_id`、`kind`（`bitstream`｜`firmware`）、`relative_path`
  （在 `results/{job_id}/` 內）、`sha256`（UNIQUE）、`size_bytes`、`target_device_kind`、
  `registered_at`、`known_good_marked_by_approval_id NULL`。
- 登記來源只有一種：`build` run 的 `output_declarations` 中 `kind=file` 且宣告 `artifact_class ∈ {bitstream, firmware}`
  （`OutputDeclaration` 新增選填欄位）的檔案，在 job-finish 結果回收後由 Server A 純函式計算 sha256、
  以內容定址複製到 `{local_home_dir}/images/{sha256}`（不可變、去重）並寫表；缺檔＝`missing`（unknown），
  不影響任務終態（同 metrics-v1 原則）。上限：單檔 ≤ `HARDWARE_IMAGE_MAX_BYTES`（建議 256 MiB），超限記 `oversize`。
- `program` 動作釘 `image_sha256`：核准時重驗該列存在且檔案 digest 一致（INV-APPROVAL-3）；派工前 Server A
  以 SFTP 把映像推到工作機 `agent_jobs/{id}/image.bin`（沿用 SFTP 落地，不經 shell），模板 argv 以
  literal 路徑引用。**映像永不從 workspace、GitHub 或 worker 自取。**
- 三張既有 artifact 表**不在本裁定內統一**：`hardware_images` 是有明確 provenance 語意的第四張表；
  統一是獨立的清理工作（ROADMAP 註記）。

### H-4 證據：沿 metrics-v1，新增 `hardware-receipt-v1`

- 建置報告（timing／utilization／編譯 log）：保留原檔於 `results/{job_id}/`（現行 rsync），數值以
  metrics-v1 寫 `metrics.json`（如 `timing.wns_ns`、`util.lut_pct`、`build.ok`）——**metrics-v1 契約不改**。
- 燒錄收據：`program`／`power` 動作的工作在 `results/{job_id}/hardware_receipt.json` 寫
  `hardware-receipt-v1`（扁平、≤16 KiB、封閉 key：`device_id`、`device_serial_observed`、`image_sha256`、
  `tool`、`tool_version`、`verify`（`verified`｜`unverified`｜`skipped`）、`exit_code`）。job-finish 以純函式解析，
  存 `hardware_receipts`（`collected`｜`missing`｜`invalid`｜`oversize`；`missing = unknown`）。
  **收據不改任務終態**（哨兵 `exit_code` 仍是唯一終態來源，INV-SSH-6）。
- HIL 結果：通過／失敗計數走 metrics-v1；JUnit／報告檔留在 results 供下載；`hil_test` 也寫收據
  （記錄測的是哪片板、哪個映像）。
- 分析規則承憲章 §8：結論指向 run／artifact／metric；缺失＝unknown。

### H-5 排程：v1 裝置是 worker 子資源，一機一件不變

- 不新增排程語意：一台 worker 同時只跑一件（含硬體工作）；裝置只是資格過濾（H-1）。
- 「一板一件」（同機多板並行）與跨機裝置池化屬未來裁定（與 `DG-GPU-SCHED` 同類）。
- v1 硬體工作只支援 **SSH 後端**（`backend: Literal["ssh"]` 不變）；Node 後端因 attempt unit 的
  `PrivateUsers`／`ProtectSystem=strict` 無法碰 `/dev/*`，留待 DG-NODE-CANARY 之後另裁
  （屆時需 `DeviceAllow=`／`SupplementaryGroups=` 的 isolation contract v2）。

### H-6 安全：以核准 kind 分級，不靠黑名單分辨燒錄

- `is_dangerous()` 黑名單**不擴張**成硬體指令辨識（`esptool erase_flash` 是合法的實體動作）；
  實體動作的可辨識性來自 `action_class` 與 `hardware_action_v2`。黑名單只補一條結構性規則：
  `action_class ∈ {compute, build}` 的模板 argv **不得**含宣告為 `program`／`power` 工具的 literal
  （工具清單由 environment revision 宣告：`physical_tools: list[str]`），違者在建卡當下 400（INV-APPROVAL-2）。
- 回退＝再燒已知良好映像：`hardware_images.known_good` 只能由人標記，且只有兩條路：
  (a) 某次 `hil_test` 核准通過且收據 `verify=verified` 後，決定者在同一決定中勾選「標記為 known-good」；
  (b) 平台管理員在 UI 直接標記（低風險筆記類例外，比照 `experiment_records`；需本裁定具名列入
  INV-APPROVAL-1 例外表）。回退動作本身仍是一張 `hardware_action_v2` 卡。
- 電源動作永遠是獨立的 `power` 卡，不與 `program` 合併成一步。
- 憑證（板子的 debug 認證、PDU 密碼）走 environment 的 `secret_references`（永不進 prompt／稽核／DB 明文），
  現行 secret-reference 後端仍 keep disabled（D4）——**v1 只支援不需要憑證的燒錄路徑**。

---

## 2. 替代方案（Alternatives）

| 題 | 建議 | 替代 | 不建議的理由 |
|---|---|---|---|
| H-1 | servers.yaml `devices:` | DB 表 + 新 `hardware_device_register` 核准 kind | 多一個 kind 與 UI；且裝置本來就是機器設定，servers.yaml 已有 revision-pinned 協議 |
| H-2 | 新 kind `hardware_action_v2` | 只在 `execution_plan_v2` 加 `action_class` 欄位 | 卡片／稽核／授權／experiment 排除都要靠欄位檢查，容易漏；新 kind 是結構性保證 |
| H-3 | 映像登記表 + Server A 內容定址保存 | metadata-only（映像留在 worker） | 燒錄要跨機（build 機 ≠ 板子所在機）；metadata-only 無法保證燒的就是核准的 bytes |
| H-5 | 一機一件不變 | 一板一件 | 需要新的排程語意與觀測，超出 v1 |

---

## 3. 明確不做（Non-goals）

任意 SSH／遠端 shell；agent 直接觸發任何實體動作；一次核准多個實體動作（matrix 批次燒錄）；
Node 後端硬體工作；需要憑證的燒錄／PDU（secret-reference 後端仍關閉）；映像推 GitHub；
自動回退；一板一件排程；三張 artifact 表統一。

---

## 4. 需要你裁定的問題（Questions）

1. **H-1** 裝置宣告放 servers.yaml `devices:`（建議）還是 DB 表 + 核准 kind？
2. **H-2** 實體動作用新 kind `hardware_action_v2`（建議）還是沿用 `execution_plan_v2` 加欄位？
3. **H-3** 映像由 Server A 內容定址保存（建議；上限 256 MiB）還是 metadata-only？
4. **第一片板子**：M5 pilot 先支援哪一種硬體與工具鏈？（例：ESP32／STM32 走 esptool／openocd；
   FPGA 走 Vivado `program_hw`／openFPGALoader）——決定第一組 presence 探測與 `physical_tools` 字面值。
5. **H-6(b)** 平台管理員 UI 直接標記 known-good 是否列為 INV-APPROVAL-1 低風險例外？
6. **順序**：建議 P1 資源模型＋探測＋`executable_present`（H-1）→ P2 `build` 模板＋映像登記（H-3）
   → P3 `hardware_action_v2`＋收據（H-2、H-4、H-6）→ P4 UI（Hardware 分頁）。各自 packet、全綠→commit→pilot。

---

## 5. 驗收（Acceptance，實作後）

1. 在 servers.yaml 宣告一片板子 → 基礎設施頁顯示裝置與在場狀態；拔掉板子 → 觀測變 absent，
   建 `program` 卡時被拒（`target_device_absent`）。
2. `build` 模板跑 synthesis → `results/{job_id}/` 回收 → `hardware_images` 出現一列（sha256、大小、來源 run）。
3. 助手／session 說「把剛剛的 bitstream 燒到 106 的板子」→ 出現 `hardware_action_v2` 待審卡
   （裝置序號、映像 digest 可見）；agent 不能核准；dev-operator 路徑不能決定；experiment matrix 含
   `program` 被拒。
4. 核准 → 工作機燒錄 → 收據 `verify=verified` 入庫；哨兵 exit_code 決定終態；收據缺失＝unknown。
5. `hil_test` → metrics-v1 入庫；決定者可標記 known-good；回退＝一張新的 `program` 卡釘舊映像。
6. Node 後端目標 → 建卡當下 400 `hardware_backend_unsupported`。
