# 不變量草案:INV-PROJECT-* / INV-DATA-* / INV-SCHED-*(Phase 0)

> **狀態:草案,尚未核准,不具約束力。**
> 依 PLAN.md Phase 0 要求草擬。經使用者明文核准後才逐條併入
> `invariants.md`(正典),屆時刪除本檔。條文對應 PLAN.md 2026-07-11
> 重寫版 §3.2、§6、§7、§8、§12;Enforcement 欄位標「規劃中」者代表
> 對應程式還沒寫,核准的是行為契約,不是既成事實。

---

## INV-PROJECT-*(專案身分與版本)

### INV-PROJECT-1(草案)Project 身分是 UUID,name 只是顯示與 slug
- **Statement**:Project 的跨系統身分是 `projects.id`(UUID);`name` 僅供顯示/slug,不再作為跨表外鍵的正式身分。過渡期由 legacy name adapter 雙讀寫,驗證完成才移除 name 引用。
- **Scope**:`app/db.py` schema、所有引用 project 的表與 API。
- **Enforcement**:規劃中(§14 切片 1 migration)。
- **Forbidden**:新表/新欄再以 name 作外鍵;migration 刪除既有 name 欄或憑空改寫歷史資料。
- **Verification**:fresh DB＋既有 DB 雙軌 migration 測試(比照 INV-STATE-3)。

### INV-PROJECT-2(草案)Hub 是 managed project 唯一正式 code source
- **Statement**:managed project 的正式 code revision 只來自 Server A bare hub 的 commit;正式 run/deploy 固定 hub commit(ProjectVersion),不得只指向「某台機器目前那份目錄」。
- **Scope**:`app/hub.py`、deploy/run 建立路徑。
- **Enforcement**:部分存在(hub sync/deploy);ProjectVersion 固定 revision 規劃中(§14 切片 4)。
- **Forbidden**:部署直接 rsync 整個 project tree 而非由 hub commit 建 bundle;用 dirty instance 跑「正式」run 而不標 `unreproducible=true`。
- **Verification**:deploy/run 建立測試斷言 commit 被固定並保存。

### INV-PROJECT-3(草案)Instance 狀態由唯讀 reconcile 得出
- **Statement**:ProjectInstance 的 `state`(available/missing/dirty/diverged/unknown)只由定期唯讀 probe 更新;連不上機器 → `unknown`,不是 missing、更不是刪除;list/detail API 不即時 SSH 全部機器。
- **Scope**:instance reconcile 迴圈、projects API。
- **Enforcement**:部分存在(dirty/last_seen);狀態機規劃中(§14 切片 2)。
- **Forbidden**:在 HTTP 請求路徑內同步 SSH 掃描;unreachable 即改寫 state 為 missing;自動刪 instance 紀錄。
- **Verification**:FakeSSH 的 unreachable → unknown、消失 → missing 分支測試。

### INV-PROJECT-4(草案)專案合併/連結由人裁定
- **Statement**:candidate 與既有 Project 的「相同」判定只提供提示(normalized git remote/歷史關係),連結或合併永遠由使用者選擇;import/link/merge 不刪原目錄、不改 working tree、不自動覆寫 dirty instance。
- **Scope**:`app/inventory.py`、candidate 匯入/連結流程。
- **Enforcement**:規劃中(§14 切片 3)。
- **Forbidden**:相同 basename/path 自動合併;非 Git 專案以內容 hash 自動宣稱相同。
- **Verification**:連結流程測試(同名不同專案不被合併、連結後原目錄不動)。

---

## INV-DATA-*(資料集/Artifact)

### INV-DATA-1(草案)DatasetSnapshot 不可變
- **Statement**:published snapshot 的內容與身分永久綁定;同 id 不得就地換內容。rolling dataset 的 `latest` 只是 pointer,job 核准當下固定 snapshot id。
- **Scope**:ArtifactStore、dataset 登記與 job 綁定。
- **Enforcement**:規劃中(§14 切片 6~7);現行 datasets 表已是 (name,version) 唯一。
- **Forbidden**:同名版本就地覆寫;job 綁 mutable latest;把舊 dataset 偽造 content hash(legacy snapshot 標 unknown)。
- **Verification**:snapshot publish/rebind 測試;legacy 登記顯示 verification level。

### INV-DATA-2(草案)完整性以 manifest＋hash＋final marker 為準
- **Statement**:replica 是否可用只看「manifest/shard hash 驗證通過＋final marker 內容(snapshot id、manifest hash、file count、logical bytes)相符」;scheduler 只認 replica ready＋marker matching。
- **Scope**:transfer/verify/publish 與 scheduler eligibility。
- **Enforcement**:規劃中;現行 manifest(檔數+大小)過渡期繼續有效,標示 verification level。
- **Forbidden**:只憑檔案數＋總大小當唯一完整性證據;把數百萬檔完整清單塞進主 DB。
- **Verification**:marker 不符/缺 marker → 不派工的測試。

### INV-DATA-3(草案)傳輸經 staging,atomic publish
- **Statement**:資料落地順序固定:staging 解包 → 驗證 → atomic rename publish → replica=ready;`(snapshot,target)` 同時最多一個 active transfer lease,服務重啟後可接手或釋放。
- **Scope**:transfer state machine。
- **Enforcement**:規劃中(§14 切片 7)。
- **Forbidden**:直接解包進 final path;中斷後重傳已完成 shard;兩個 transfer 同時寫同一 target。
- **Verification**:中斷/重啟/重複 lease 測試。

### INV-DATA-4(草案)資料清除走 approval
- **Statement**:dataset、result、artifact 的刪除/eviction 一律建立 approval,且核准前檢查 job reference、last used 與 quota;系統永不自動刪。
- **Scope**:所有 cleanup/eviction 路徑。
- **Enforcement**:與 INV-APPROVAL-1 同閘門;cleanup 功能規劃中。
- **Forbidden**:磁碟壓力觸發的自動刪除;cleanup 繞過 reference check。
- **Verification**:有 reference 的 snapshot 清除請求被拒的測試。

---

## INV-SCHED-*(排程)

### INV-SCHED-1(草案)排程決策是純函式、可解釋
- **Statement**:eligibility filter 與 scoring 都是純函式或可重現 policy,輸入只有持久化的 job/worker/dataset/policy 資料;每個 queued job 都有結構化 reason code(PLAN.md §8.3 清單)。
- **Scope**:`app/scheduler.py` 與後續 Scheduler v2。
- **Enforcement**:部分存在(priority/FIFO 純函式);reason code 規劃中。
- **Forbidden**:LLM 參與 filter/score;依未持久化的即席狀態做決策;無法回答「為什麼還沒跑」的 queued job。
- **Verification**:純函式單元測試;reason code 對照測試。

### INV-SCHED-2(草案)核准的 plan 不得偷換
- **Statement**:approval 固定 ExecutionPlan hash;核准後 target、dataset transfer、code version 或 command 任一改變 → 原 approval 失效,必須產生新 plan 重審。scheduler 不得在核准後創造 plan 未顯示的 sync/deploy/setup 副作用。
- **Scope**:Planner/approval/dispatch 路徑。
- **Enforcement**:規劃中(§14 切片 8~9);與 INV-APPROVAL-3 的核准時重驗互補。
- **Forbidden**:核准後靜默重選 worker;把未顯示的 staging 動作夾帶進 dispatch。
- **Verification**:plan hash 不符 → 拒絕執行的測試。

### INV-SCHED-3(草案)資源語意=整機獨占,直到明文核准
- **Statement**:現行資源模型是一台 worker 一個 ordinary job;GPU slot、preemption、migration、quota 都不因 schema 有欄位(如 `gpus_needed`)就實作,啟用任何一項是架構決策,需使用者明文核准。
- **Scope**:scheduler、resource model。
- **Enforcement**:現行行為即如此;CLAUDE.md 已明文。
- **Forbidden**:「順手」讓兩個 job 同時派給一台機;把欄位存在當成功能授權。
- **Verification**:一機一 job 的既有測試不得弱化(INV-TEST-2)。

### INV-SCHED-4(草案)過期 observation 不派工
- **Statement**:worker observation 有 freshness;超過閾值視同 offline → skip,不派工也不判 failed(與 INV-SSH-7 一致)。DB reservation/running 先寫,遠端副作用後行(與 INV-STATE-2 一致)。
- **Scope**:monitor、scheduler eligibility。
- **Enforcement**:部分存在(online 判定);freshness 閾值規劃中(§14 切片 10)。
- **Forbidden**:用 stale observation 當即時資源證據;unreachable 期間改動 job 狀態。
- **Verification**:stale observation → 不入 eligible 集合的測試。
