# Glossary(專案詞彙表)

| 詞彙 | 意義 |
|---|---|
| **哨兵檔案協議**(sentinel protocol) | 任務在工作機的 `agent_jobs/{id}/` 目錄:`cmd.sh`(指令原文,SFTP 寫入)、`run.sh`(`bash cmd.sh > job.log 2>&1; echo $? > exit_code`)、`job.log`、`exit_code`。成敗判定只看 `exit_code` 檔案。定義:`app/jobqueue.py` |
| **reconcile** | 每輪排程對 running 任務依哨兵協議三分支判定:exit_code 在→done/failed;tmux session 在→running;都不在→requeue。tmux GONE 後會二查 exit_code 防競態 |
| **資料引力**(data gravity) | `pick_job()` 的**硬性資格過濾**:需要資料集的訓練任務只派給 `dataset_cache` 已登記該資料集的機器;哪台都沒有時留在 queued,不是「排後面」 |
| **`_local`** | sync/setup 類任務的特殊執行目標=Server A 本地(`app/localrun.py`,本地 tmux),永遠在線、可並行(上限 `LOCAL_SYNC_CONCURRENCY`) |
| **approval kind** | `VALID_APPROVAL_KINDS`(`app/db.py`)中的一種寫入動作類型:enqueue、stop、import_project、ignore_project_candidate、inventory_scan、server_add/update/disable/delete、apply_patch、coding_task、ignore_nested_candidates、git_init、project_deploy |
| **source 標記** | 請求來源通道:`web`/`chatgpt`/`vllm`/`api`。只是稽核與 UX 用途,不是權限層 |
| **WEB_DIRECT_EXECUTE** | `.env` 開關(預設 true):web 來源的 enqueue/stop 建卡後同請求內自動 `approve()`(提案者=批准者),`approved_by: "web-direct"` |
| **auto_approve.yaml** | 使用者預先寫的確定性自動核准規則(source/kind/command_regex/project/pin_server 五欄 AND,規則間 OR);kind 白名單只有 enqueue/stop;黑名單指令救不回 |
| **manifest** | 資料集的「檔案清單+各檔大小」JSON(代替全量 hash);sync 後比對檔數/總大小,通過才登記 `dataset_cache` |
| **dataset card**(資料卡) | 資料集版本的 description/method/derived_from/counts 紀錄;無卡的舊版本查詢回自動事實+明確「無紀錄」,agent 不得腦補 |
| **stalled_suspect** | 卡死偵測旗標:`job.log` 超過 `STALL_MINUTES` 未增長。只標旗標+寄一次信,**不改 status、不殺任務** |
| **Codex Runner** | `.env` `CODEX_RUNNER_SERVER` 指定的唯一一台跑 `type="coding"` 任務(codex exec)的機器;`codex_runner_reserve` 控制它是否兼接一般任務 |
| **coding run** | `coding_runs` 表的一筆 AI 改碼執行紀錄(instruction→worktree→result branch→bundle) |
| **hub** | Server A 上的中央 bare repo(`~/git/{project}.git`);worker→hub 用 bundle 拉回,hub→worker 部署也走 bundle(工作機互不相通,Server A 中轉) |
| **project instance** | 某專案在某台機器某路徑的一份存在(`project_instances` 表,穩定 hash id,upsert) |
| **candidate**(候選專案) | inventory 掃描找到、待人工核准匯入的目錄(`project_candidates` 表,pending/imported/ignored) |
| **embedded dataset** | 專案目錄底下的資料目錄(data/datasets/…):只統計不讀內容,不進 datasets/dataset_cache 管理、不自動同步 |
| **MCP bridge** | `app/mcp_bridge.py` 獨立行程(預設 8890),ChatGPT custom connector 經 Cloudflare Tunnel 接入;唯讀工具+request_* 工具,永無 approve |
| **鐵律** | 原始規格第 2 節的底線,常被引用的四條:(1) LLM 缺席不影響本體 (2) 一切派工經核准、危險指令直接拒 (3) 每個動作進稽核 (4) 服務只綁私網 |
