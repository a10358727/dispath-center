# 實作指令：AI 訓練調度中心（給 Claude Code）

你是在 Server A 上工作的工程師。請照本文件實作一套「AI 訓練調度中心」：
監控多台伺服器、讓機器不空轉、以專案為中心的網頁介面、資料自動搬移、
跑完寄 email。**請分階段實作，每個階段通過驗收標準後再進入下一階段。**

---

## 1. 系統目標

- 我有多台伺服器（會持續增加），主要跑 AI 訓練，資料集很大（幾十到幾百 GB）。
- 我要一個網頁：看到每台伺服器狀態、每個專案狀態，用對話或按鈕把專案派去跑。
- 排程器要讓伺服器不空轉：有空機就自動從佇列派下一個任務。
- 任務跑完自動寄 email、把結果收回 Server A。
- 資料搬移要聰明：優先派給「已經有這份資料」的機器，需要時才 rsync。

## 2. 鐵律（違反視為實作錯誤）

1. **LLM 不進排程迴圈。** 監控、挑機器、派工、判斷完成，全部用確定性規則實作。
   LLM 只用在：理解使用者的自然語言、失敗診斷。LLM 不可用時系統照常運作。
2. **寫入型動作一律先核准。** 排任務、同步資料要在介面上按「核准」才生效；
   指令含 `rm -rf`、`dd`、`mkfs`、`shutdown`、`reboot`、`> /dev/sd`、`userdel`
   直接拒絕，不給核准機會。
3. **每個動作寫稽核。** 派工、完成、核准、拒絕、同步，append 到 `audit.jsonl`
   （每行一筆 JSON：時間、動作、參數、結果）。
4. **服務只綁私網。** uvicorn 綁 Tailscale 私網 IP 或 127.0.0.1，不綁 0.0.0.0 對公網。
5. **不自己發明工作。** 佇列空了機器閒著沒關係，只能從我維護的「低優先度待辦池」補位。

## 3. 技術選型

- Python 3.10+，FastAPI + uvicorn，WebSocket 做聊天與即時更新
- SSH 用 `asyncssh`（金鑰認證，路徑從設定讀）
- 持久化用 SQLite（任務、專案、資料集註冊表）；設定用 YAML
- 前端：單檔 `index.html`，原生 JS + fetch/WebSocket，不用打包工具
- LLM（選用）：`anthropic` SDK，環境變數 `ANTHROPIC_API_KEY` 沒設就走規則式解析
- 用 systemd service 常駐，`Restart=always`

## 4. 設定檔與目錄約定

`servers.yaml`（機器清單，加機器只改這裡）：
```yaml
servers:
  - name: server-b
    host: 100.x.y.z        # Tailscale IP
    user: train
    key: ~/.ssh/agent_key
    gpu: true
    idle_gpu_util: 15      # GPU% 低於此值視為空閒
    tags: [gpu, training]
```

每台工作機的目錄約定（實作時自動 `mkdir -p`）：
- `~/datasets/{資料集名}/{版本}/` —— 資料集，版本是一級概念
- `~/projects/{專案名}/` —— 程式碼（用 git clone/pull 同步，不用 rsync）
- `~/agent_jobs/{任務id}.log` —— 任務 log 與退出碼
- `~/results/{任務id}/` —— 產出物，跑完 rsync 回 Server A 的 `~/results/`

## 5. 核心模組需求

### 5.1 資源監控（monitor）
- 每 20 秒對每台 SSH 執行：`nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits; cat /proc/loadavg`
- 記錄：online、GPU 使用率（多卡取最大）、VRAM、load1、更新時間、錯誤訊息
- 空閒判定：在線 且 沒有本系統派的任務在跑 且（GPU 機：GPU% < idle_gpu_util；
  CPU 機：load1 < idle_load）
- 連線失敗標離線，排程器跳過離線機，不誤判任務失敗

### 5.2 專案與資料集註冊表（registry）
- 專案：名稱、git repo 或路徑、使用的資料集名＋版本、預設訓練指令、require_tag
- 資料集：名稱、版本、大小、來源路徑（Server A 上）、checksum（大檔可用
  「檔案清單＋各檔大小」代替全量 hash，註明取捨）
- **快取地圖**：記錄每台機器已有哪些「資料集＠版本」。來源兩個：
  同步任務成功後登記；每小時對各機 `ls ~/datasets/*/*/` 校正一次

### 5.3 任務佇列（queue，SQLite）
- 欄位：id、type（train / sync / adhoc）、project、command、require_tag、
  pin_server（指定機器，空=自動）、depends_on（依賴的任務id）、status
  （queued/running/done/failed/blocked）、server、priority（normal/low）、
  created/started/finished、exit_code、log_tail
- 依賴：depends_on 未 done 的任務不可派發；依賴 failed 則本任務標 blocked
- 服務重啟時，先前 running 的任務先查目標機 tmux session 是否還在：
  在→接回追蹤；不在→重新排隊

### 5.4 排程器（scheduler，每 10 秒一輪）
- 對每台空閒機器挑任務，順序：
  1. 符合 pin_server / require_tag / 依賴已完成
  2. **資料引力**：優先挑「這台已快取所需資料集」的任務
  3. priority normal 優先於 low（待辦池），同級 FIFO
- 挑到「目標機沒有所需資料集」的訓練任務時：自動建一個 sync 任務
  （rsync 從 Server A 推過去），把訓練任務 depends_on 指向它
- 派發方式：`mkdir -p ~/agent_jobs && tmux new -d -s job_{id} '( {command} ) > ~/agent_jobs/{id}.log 2>&1; echo JOB_EXIT:$? >> 同一個log'`
- 完成偵測：`tmux has-session -t job_{id}` 不存在＝結束；抓 log 尾 40 行，
  解析 `JOB_EXIT:` 判定成敗；SSH 連不上時跳過本輪，不判定
- 任務結束 hook：更新狀態→寄 email→rsync 拉回 `~/results/{id}/`→寫稽核

### 5.5 資料同步（sync 任務）
- 指令：`rsync -a --partial --info=progress2` 從 Server A 推到目標機
- 同步前先在目標機 `df` 檢查剩餘空間 > 資料集大小 × 1.2，不足則任務失敗並說明
- 同步後驗證（檔案數與總大小比對），成功才登記進快取地圖
- 同步進度要能在介面上看到（讀 log 尾即可）

### 5.6 Email 通知（mailer）
- SMTP，環境變數：SMTP_HOST/PORT/USER/PASS、MAIL_FROM、MAIL_TO；沒設就跳過
- 內容：任務名、專案、機器、成敗、exit code、耗時、結果路徑、log 尾 40 行
- 若有 ANTHROPIC_API_KEY，在信件開頭加三行以內的 LLM 摘要（失敗時說可能原因）

### 5.7 聊天 agent（chat）
- WebSocket。有 API key：把使用者訊息＋目前伺服器/專案狀態餵給 LLM，
  輸出結構化 JSON intent（status / jobs / enqueue / chat）
- 沒 key 的規則式後備：「狀態」「任務」「跑 <指令>」三種要能動
- enqueue 一律回「待核准」卡片（顯示完整指令與資料同步計畫），核准才入佇列

### 5.8 失敗診斷（有 API key 才啟用）
- 任務 failed 時提供「診斷」按鈕：把 log 尾＋專案結構餵給 LLM，產出
  診斷說明與修改建議（diff 形式），顯示在介面上等我決定
- 只建議、不自動改碼不自動重跑；重試上限 2 次；diff 進稽核

## 6. 網頁介面（單頁，分四個分頁）

- **總覽**：伺服器卡片網格——名稱、狀態徽章（空閒=綠/訓練中=藍/離線=紅）、
  GPU% 進度條、VRAM、目前任務、**已快取資料集的小標籤**。下方最近事件流。
- **專案**：專案列表——名稱、資料集＋大小、最近一次執行狀態、「派工」按鈕。
  派工彈窗：選機器（每台旁邊即時標註「已有資料 ✓ 免同步」或
  「需同步 82 GB ≈ 預估時間」或「忙碌，將排隊」，也可選「自動」讓排程器挑）、
  完成後選項（寄信、拉回結果）、確認即建立任務（含自動掛依賴的 sync 任務）。
- **任務**：佇列表格——狀態、專案、機器、耗時，點開看 log 尾、可取消排隊中任務。
- **對話**：類 GPT 聊天介面，含待核准卡片（核准/拒絕按鈕）、即時輸出。
- 前端每 5 秒輪詢 /servers /jobs /events；聊天走 WebSocket。
- 風格：乾淨、留白、中文介面，不要花俏。

## 7. REST/WS API（最小集合）

GET /servers、GET /projects、GET /jobs、GET /jobs/{id}/log、GET /events、
GET /audit、POST /projects（建專案）、POST /dispatch（派工，回待核准）、
POST /approve/{id}、POST /reject/{id}、POST /jobs/{id}/cancel、WS /ws

## 8. 實作順序與驗收標準

**階段 1 — 監控＋佇列＋排程（先不接 LLM、不做資料同步）**
驗收：servers.yaml 兩台機；丟 3 個 `sleep 60` 任務，全自動依序派給空機跑完；
中途重啟服務，任務不遺失；離線機被跳過；audit.jsonl 有完整紀錄。

**階段 2 — 網頁總覽＋任務頁＋核准流**
驗收：瀏覽器能看到伺服器卡與任務表；從介面排任務必經核准；
`rm -rf /tmp/x` 被直接拒絕。

**階段 3 — 專案、資料集註冊表、sync 任務、資料引力**
驗收：建一個含 1 GB 測試資料集的專案；派給「沒有資料的機器」會自動先跑
sync 再跑訓練；再派一次時因快取地圖命中而免同步；派工彈窗正確顯示
「免同步 / 需同步 N GB」。

**階段 4 — Email＋結果回收**
驗收：任務完成收到信，內容欄位齊全；`~/results/{id}/` 出現在 Server A。

**階段 5 — LLM：自然語言排程＋失敗診斷＋信件摘要**
驗收：「用 defect-v3 訓練 resnet50，要 GPU 機」→ 產生正確的待核准任務；
故意弄一個會失敗的任務，診斷按鈕給出合理分析與 diff；沒有 key 時
前四階段功能完全不受影響。

**階段 6 — systemd 常駐**
驗收：開機自動啟動；kill 後自動重啟且任務狀態正確恢復。

## 9. 明確不要做的事

- 不要用 Docker/K8s（現階段），不要引入 message queue，不要用前端框架打包
- 不要把任何密碼/金鑰寫進程式碼或 git（用 .env，提供 .env.example）
- 不要讓排程器在佇列空時自行生成任務
- 不要在未經核准的情況下執行任何寫入型指令

## 10. 交付物

專案結構清楚分模組；README 含：安裝步驟、servers.yaml 與 .env 設定說明、
每個階段的驗收操作步驟；核心邏輯（空閒判定、依賴、資料引力挑選、
危險指令攔截）要有 pytest 單元測試。
