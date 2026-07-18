# DG-C 草稿:`INV-SSH-1` 修訂與 `INV-NODE-*` 保護不變量(尚未生效)

> Status: **草稿,等待 DG-C 具名裁定(核准計畫檔 G3 閘門)**。本文件本身
> 不是不變量——`.claude/skills/dispatcher-domain/references/invariants.md`
> **完全未被修改**,在使用者具名裁定本文字(或其修訂版)之前,現行
> `INV-SSH-1`(工作機無 agent、依賴封頂)持續完整生效,任何 Node Agent
> 實作(含 C1 之後的切片)都不得動工。
>
> 依據:`docs/GOAL_3_FUTURE_WORK_PLAN.md` Phase C C0、
> `docs/CODEX_ROADMAP_PROPOSAL.md` §3.4 與 Phase 2–4 條款。
> 裁定方式:使用者審閱後在 `docs/DECISIONS.md` 記錄「DG-C 核准(文字以
> ____ 版本為準)」,屆時才把 §2/§3 的條文原樣寫入 invariants.md。

## 1. 修訂動機(為什麼要動保護不變量)

現行 `INV-SSH-1` 把「工作機上沒有本系統常駐程式」定為架構不變量。SSH
輪詢模式的天花板(斷線語意含糊、每機一連線的擴充性、無雙向事件)已在
roadmap §3.4 記錄。引入 Node Agent **必然**與 INV-SSH-1 的字面衝突,
因此依 CLAUDE.md 的規則,這是一次需要明確使用者核准的保護不變量修訂,
不與任何實作綁定核准。

## 2. 修訂後的 `INV-SSH-1`(提案文字)

### INV-SSH-1 SSH 後端無 agent、依賴封頂(修訂版)

- **Statement**:SSH 執行後端不依賴工作機上任何本系統常駐程式;遠端依賴
  只有 `tmux`、`bash`、`nvidia-smi`(GPU 機),一切互動經 asyncssh
  (`app/sshpool.py`)。rsync 由 Server A 端發起。工作機上「可以」另外
  存在經 `INV-NODE-*` 管理的 Node Agent,但 **SSH 後端的行為與依賴永遠
  不得假設它存在**;SSH 後端永久保留為每台工作機的相容/緊急通道,不因
  Node Agent 上線而移除或弱化。
- **Scope**:所有產生遠端指令的模組(不變)。
- **Enforcement**:架構慣例;`servers.yaml.example` 與 README 明文;
  ExecutionBackend seam(C1)以 golden tests 證明 SSH 路徑指令字串逐字
  不變。
- **Forbidden**:SSH 後端的遠端指令引入其他工具依賴(python、jq、
  curl…);SSH 後端假設/呼叫任何 agent 端點;**以 Node Agent 存在為由
  刪除或跳過任何 INV-SSH-2…9 的保證**。
- **Verification**:code review;新遠端指令的配對測試斷言完整指令字串;
  C1 golden tests。

(其餘 `INV-SSH-2` 至 `INV-SSH-9` 一字不動,繼續約束 SSH 後端。)

## 3. 新增 `INV-NODE-*`(提案文字)

### INV-NODE-1 Agent 身分與出站單向連線

- **Statement**:Node Agent 是**非 root** 的小型常駐服務,只發起**出站**
  已驗證 HTTPS 輪詢;工作機不得開放任何入站控制埠。每個 agent 持有一組
  可個別撤銷的 node credential,control plane 對每個請求驗證 node 身分;
  credential 洩漏的處置是撤銷該 node,不影響其他 node。
- **Forbidden**:入站 listener、共享 credential、以 IP/hostname 取代
  credential 驗證、agent 以 root 執行。
- **Verification**:agent 套件測試(全 fake、不碰真機);authorization
  catalog 覆蓋 node 端點。

### INV-NODE-2 Lease/acknowledgement 執行語意

- **Statement**:一次 attempt 只能被一個 agent lease;agent 必須先原子性
  acknowledge 並把 attempt 身分持久化到本機,才能啟動任何副作用。
  control plane 在 lease 未過期且未收到終態前,**不得**把同一 attempt
  再派給任何通道(含 SSH)——「agent 可能已啟動」期間的重複派發是被
  禁止的。
- **Forbidden**:無 lease 的執行、ack 前產生副作用、lease 期內重複派發。
- **Verification**:協議層 fake 測試覆蓋 lease 競態、重複 ack、過期
  reclaim。

### INV-NODE-3 指令位元組非插值落地

- **Statement**:核准的指令位元組由 agent 寫入檔案後,以只含已驗證識別
  字的 launcher 啟動——與 `INV-SSH-2`/`INV-SSH-3` 同構:自由文字永不
  拼進任何 shell 字串,digest 與核准 payload 綁定。
- **Forbidden**:agent 端任何形式的指令字串插值;執行未經核准 digest
  比對的位元組。
- **Verification**:launcher 純函式測試;digest 比對測試。

### INV-NODE-4 心跳過期＝unknown,不是 failed

- **Statement**:心跳過期、agent 連不上、輪詢中斷一律判 `unknown`,不得
  推斷任務失敗(與 `INV-SSH-7`/現行 unreachable≠failed 同構)。狀態
  收斂唯一依據是 agent 回報的持久化終態或(SSH 相容通道的)哨兵檔案。
- **Forbidden**:以心跳缺席把 running 任務標 failed;以 unknown 觸發
  自動重派。
- **Verification**:reconciliation 測試覆蓋 agent 消失/重啟/回歸各情境。

### INV-NODE-5 重啟不重複、狀態可收斂

- **Statement**:control plane 或 agent 任一方重啟後,已 acknowledge 的
  attempt 不得被重複啟動;雙方各自以持久化紀錄(DB/本機 attempt 檔)
  收斂,收斂規則必須可測(fake 時序測試)。
- **Forbidden**:以記憶體狀態判斷 attempt 歸屬;重啟後自動重跑未確認
  終態的 attempt。
- **Verification**:雙側重啟矩陣的 fake 測試(roadmap Phase 3 的量化
  門檻:≥100 jobs/≥2 nodes/7 天零重複啟動零假失敗)。

### INV-NODE-6 逐台提升、隨時回退

- **Statement**:Node Agent 以**每台工作機**為單位明確啟用;未啟用的
  機器完全走 SSH 後端。任何一台可在不影響其他機器的情況下回退到 SSH;
  Codex Runner 的遷移放在所有普通 worker 之後(C4 通過才動)。
- **Forbidden**:全域一刀切開關;移除 SSH 後端程式碼;讓回退需要資料
  遷移。
- **Verification**:per-node 開關測試;回退演練紀錄。

## 4. 裁定之後(不屬於本次裁定的部分)

- C1(ExecutionBackend seam,SSH 實作零行為變更+golden tests)在 DG-C
  裁定後才動工;C2–C4 依 `docs/GOAL_3_FUTURE_WORK_PLAN.md` 的量化門檻
  逐步推進,各自仍受上述不變量約束。
- agent 套件的發佈/安裝方式、node credential 的簽發流程屬 C2 的實作
  設計,屆時以本文件的不變量為邊界,不需要再次修訂不變量。
