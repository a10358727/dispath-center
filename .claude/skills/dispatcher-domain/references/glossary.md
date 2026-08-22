# Glossary(專案詞彙表)

| 詞彙 | 意義 |
|---|---|
| **哨兵檔案協議**(sentinel protocol) | 任務在工作機的 `agent_jobs/{id}/` 目錄:`cmd.sh`(指令原文,SFTP 寫入)、`run.sh`(`bash cmd.sh > job.log 2>&1; echo $? > exit_code`)、`job.log`、`exit_code`。成敗判定只看 `exit_code` 檔案。定義:`app/jobqueue.py` |
| **reconcile** | 每輪排程對 running 任務依哨兵協議三分支判定:exit_code 在→done/failed;tmux session 在→running;都不在→requeue。tmux GONE 後會二查 exit_code 防競態 |
| **資料引力**(data gravity) | `pick_job()` 的**硬性資格過濾**:需要資料集的訓練任務只派給 `dataset_cache` 已登記該資料集的機器;哪台都沒有時留在 queued,不是「排後面」 |
| **`_local`** | sync/setup 類任務的特殊執行目標=Server A 本地(`app/localrun.py`,本地 tmux),永遠在線、可並行(上限 `LOCAL_SYNC_CONCURRENCY`) |
| **approval kind** | `VALID_APPROVAL_KINDS`(`app/db.py`)中的一種 material 寫入動作類型。清單會隨切片成長,**以 `app/db.py` 為準**,不要引用文件裡的複本;自動核准白名單永遠只有 enqueue/stop |
| **source 標記** | 請求來源通道:`web`/`chatgpt`/`vllm`/`api`。只是稽核與 UX 用途,不是權限層 |
| **WEB_DIRECT_EXECUTE** | `.env` 開關(預設 true):web 來源的 enqueue/stop 建卡後同請求內自動 `approve()`(提案者=批准者),`approved_by: "web-direct"` |
| **auto_approve.yaml** | 使用者預先寫的確定性自動核准規則(source/kind/command_regex/project/pin_server 五欄 AND,規則間 OR);kind 白名單只有 enqueue/stop;黑名單指令救不回 |
| **manifest** | 資料集的「檔案清單+各檔大小」JSON(代替全量 hash);sync 後比對檔數/總大小,通過才登記 `dataset_cache` |
| **dataset card**(資料卡) | 資料集版本的 description/method/derived_from/counts 紀錄;無卡的舊版本查詢回自動事實+明確「無紀錄」,agent 不得腦補 |
| **stalled_suspect** | 卡死偵測旗標:`job.log` 超過 `STALL_MINUTES` 未增長。只標旗標+寄一次信,**不改 status、不殺任務** |
| **Codex Runner** | `.env` `CODEX_RUNNER_SERVER` 指定的唯一一台跑 `type="coding"` 任務(codex exec)的機器;`codex_runner_reserve` 控制它是否兼接一般任務。名稱是現行部署事實——抽象層叫 Development Agent runner,不綁 Codex |
| **coding run** | `coding_runs` 表的一筆 AI 改碼執行紀錄(instruction→worktree→result branch→bundle) |
| **hub** | Server A 上的中央 bare repo(`~/git/{project}.git`);worker→hub 用 bundle 拉回,hub→worker 部署也走 bundle(工作機互不相通,Server A 中轉) |
| **project instance** | 某專案在某台機器某路徑的一份存在(`project_instances` 表,穩定 hash id,upsert) |
| **candidate**(候選專案) | inventory 掃描找到、待人工核准匯入的目錄(`project_candidates` 表,pending/imported/ignored) |
| **embedded dataset** | 專案目錄底下的資料目錄(data/datasets/…):只統計不讀內容,不進 datasets/dataset_cache 管理、不自動同步 |
| **MCP bridge** | `app/mcp_bridge.py` 獨立行程(預設 8890),ChatGPT custom connector 經 Cloudflare Tunnel 接入;唯讀工具+request_* 工具,永無 approve |
| **鐵律** | 原始規格第 2 節的底線,常被引用的四條:(1) LLM 缺席不影響本體 (2) 一切派工經核准、危險指令直接拒 (3) 每個動作進稽核 (4) 服務只綁私網 |
| **Development Agent** | Development Plane 的受控改碼代理抽象。Codex 是現行唯一 provider;Claude Code 與未來 coding agent 是已接受方向。所有 provider 受同一套 safety boundary,選 provider 不是權限提升 |
| **AgentProvider** | 一個 Development Agent 的 provider-specific adapter,只能存在於 reviewed allowlist registry(`app/coding_agents.py`);provider CLI 細節不得進核心 domain model |
| **AgentSession** | 與某 provider 的一次工作階段(概念,尚未實作;不要預先發明其狀態機) |
| **Development Plane** | 「應該存在什麼程式碼」那一側:Project onboarding、隔離 workspace/worktree、改碼/測試/diff、ProjectVersion。定義見 `development-platform.md` |
| **Compute Plane** | 「要跑什麼、跑在哪、用什麼資料」那一側:Dataset、ExecutionPlan、Approval、Scheduler、SSH/Node 後端、Run、Results/Artifacts |
| **promotion** | 兩個 plane 的唯一交會點:人工核准 `engineering_task_promote` → 本地 bundle 驗證 → 不可執行的 ProjectVersion → 發布本地 hub ref。永不自動核准、永不推 GitHub |
| **ProjectVersion** | 不可變的程式碼版本身分。有 promotion approval 的才可支撐 reproducible run;沒有的是 `legacy_observed` |
| **ExecutionPlan** | 把 code revision/environment/run template/dataset/resource 釘成一份不可變執行意圖;核准後最多具現化一個 Job(預設關閉) |
| **default-off** | 程式已實作但乾淨設定下不啟用。已實作 ≠ 已啟用 ≠ 已部署 ≠ production-ready(見 `docs/CAPABILITY_LEDGER.md`) |
