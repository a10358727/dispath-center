PR-08 Durable Audit / Export Outbox 審核意見

依據：docs/IMPLEMENTATION_PROGRESS.md 的 PR-06～PR-08 紀錄。限制：目前連線到的 GitHub main 尚未看到 PR-08 對應 commit，因此以下是資料模型與遷移策略審核，不是逐行程式碼 review。

結論

方向正確，但目前只能標示為：

durable audit foundation complete / partial adoption / not full audit migration

不應標示為所有 domain mutation 都已具備 transactional audit。

已採用且合理的設計：

audit_events 作為 append-only ledger。

domain mutation、audit event、export outbox 同一 transaction。

JSONL 是 export sink，不是 migrated path 的權威來源。

不把舊 JSONL 回填成虛構的 actor、resource、timestamp provenance。

export failure 不刪除 DB event。

backup／restore 驗證 hash chain。

audit API 使用 bounded reads。

一、Schema v2 必須確認

1. Audit Event 不可修改

SQLite 必須用 trigger 禁止：

UPDATE audit_events

DELETE FROM audit_events

修改 predecessor、payload、actor、resource、timestamp 或 hash

不能只依賴 repository 沒提供 update 方法。

2. Hash 必須使用 Canonical Bytes

Hash envelope 必須固定：

contract version

event ID

UTF-8

key sorting

compact JSON separators

timestamp 格式

null／empty semantics

actor／action／resource／result／payload

previous event hash

一般 json.dumps() 預設輸出不足以作長期證據格式。

3. Chain Append 必須序列化

建立 event 時應在同一個 BEGIN IMMEDIATE：

讀取最後 event hash。

寫 domain mutation。

寫 audit event。

寫 export outbox。

commit。

需要兩個 SQLite connection 的並行測試，確認 chain 不分叉，而且 rollback 後三者都不存在。

4. Event 與 Export Operation 必須 1:1

建議：

UNIQUE(audit_event_id)
FOREIGN KEY(audit_event_id)
  REFERENCES audit_events(id)
  ON DELETE RESTRICT

Outbox 狀態應使用 CHECK，例如：

pending
processing
retry_wait
delivered
dead_letter

欄位至少包含 attempt count、available time、lease owner、lease expiry、safe error category、delivered time。

5. Outbox Claim 必須是 Atomic CAS

不可先 SELECT 再 UPDATE。

驗收：

兩個 worker 同時 claim，只有一個成功。

worker crash 後 lease 到期可重取。

delivered 不可重取。

超過 retry ceiling 進 dead-letter。

dead-letter 只能由明確 operator action replay。

6. JSONL Export 的 Crash Window

必須處理：

JSONL append 成功
→ process crash
→ DB 尚未標 delivered
→ restart 後再次 append

早期可採 at-least-once export，但每行必須帶穩定的：

event_id

event_sha256

reader／importer 必須可以 deduplicate。文件不得宣稱 exactly-once。

7. Hash Chain 不等於真實性證明

若攻擊者能改完整 SQLite，也能重算整條 chain。

因此 production gate 應增加外部 anchor：

每日／每 segment 最後 event hash。

off-host immutable storage。

獨立簽署 checkpoint。

backup manifest pin 住 segment digest。

目前 backup restore 驗證只能證明備份內部一致，不能證明 DB 從未被整體改寫。

8. Retention 必須保存 Chain Anchor

不可直接刪除舊 audit row。

應採 segment archive：

first hash

last hash

previous segment hash

archive checksum／signature

明確 retention policy

9. Secret／PII 必須在寫入前最小化

Audit 是不可變資料，禁止寫入：

token、cookie、Authorization header

password、private key

raw environment

raw command output

raw exception

未經必要性評估的個資

採 allowlist，只保存 actor ID、action code、resource ID、approval ID、request ID、safe reason code、digest 與 bounded evidence。

10. 排序使用 Event ID

recorded_at 使用 DB transaction time。

occurred_at 可選。

API cursor 使用 event ID。

不使用 timestamp 作唯一順序依據。

二、新 UoW 與 Legacy JSONL 雙軌問題

分階段接線可以接受，但必須明確定義權威來源：

Mutation 類型

權威來源

JSONL

已遷移 UoW

audit_events

只由 outbox export

未遷移 Legacy

無 durable ledger

append_audit() best-effort

Read-only observation

telemetry 或另定 policy

不可冒充 domain audit

必須避免 Double Write

已遷移路徑不得同時：

transaction 內寫 audit_events；

transaction 外直接呼叫 append_audit()。

否則一個操作會產生兩筆看似獨立的 audit。

API 必須標示證據品質

若 /events 或 /audit 同時顯示兩種資料，必須包含：

{
  "source": "durable_db",
  "durability": "transactional"
}

或：

{
  "source": "legacy_jsonl",
  "durability": "best_effort"
}

並回傳目前 coverage：

{
  "audit_coverage": {
    "mode": "partial",
    "durable_actions": [],
    "legacy_actions": [],
    "required_durable_actions": [],
    "required_legacy_actions": [],
    "required_missing": []
  }
}

建立 Machine-readable Adoption Catalog

例如：

AUDIT_ADOPTION = {
    "execution_attempt.create": "durable",
    "node_attempt.create": "durable",
    "project.update": "legacy",
    "approval.decide": "legacy",
}

CI 檢查：

每個 mutation route 都已登錄。

durable action 不得呼叫 legacy append。

新增 mutation 預設必須 durable。

legacy action 必須有 migration issue 與 owner。

建議遷移順序

Approval／Authorization／Identity。

Server／Node／Configuration mutation。

Project／Run／Stop／Result。

Dataset／Engineering／Promotion。

每個切片獨立 PR，完成：

domain transaction 接 UoW；

transaction 內 append durable event；

移除直接 JSONL append；

fault injection；

adoption catalog 更新；

API coverage 更新。

不要將舊 JSONL 每行回填成 durable event。最多只封存舊檔的 file hash、時間範圍與 line count。

三、必加測試

Chain／Schema

concurrent append 不分叉

rollback 不留 event／outbox

UPDATE／DELETE trigger

predecessor tamper

payload tamper

missing middle event

genesis event

schema metadata mismatch

Outbox

two-worker claim race

crash before export

crash after append before receipt

lease expiry recovery

retry ceiling

dead-letter／manual replay

disk full／permission denied

partial final JSONL line

restart recovery

duplicate deduplication

Adoption

durable action 不呼叫 legacy append

legacy action 不冒充 durable

rollback 不產生 event

successful mutation 恰好一個 event

route catalog 與 audit catalog coverage

API 顯示 source／durability

backup restore 後 chain 與 outbox state 正確

四、Quickstart Cleanup Timeout

目前不能把完整 suite 標成綠燈。

「主機高負載」是合理假設，但仍需驗證。

在 idle host 執行：

uptime
ps -eo pid,ppid,stat,etimes,cmd --forest
DISPATCH_TEST_NETWORK=deny python -m pytest -q

最低要求：

完整 suite 連續 2 次通過。

tests/test_quickstart_script.py 單獨連跑 10 次。

記錄 CPU、RAM、load average、I/O pressure。

失敗時保存 process tree。

timeout 後確認無 orphan process。

cleanup 應處理 process group，不只 kill parent。

只有 idle host 完整 suite 穩定通過、quickstart 10 次全過、無 orphan child，才能將先前 4 個 timeout 歸類為 environmental。

若 idle host 仍失敗一次，就應視為 cleanup race。

五、合併 Gate

實際 schema migration 已逐行 review。

audit_events UPDATE／DELETE 被 DB 阻擋。

canonical hash format 有版本。

concurrent append 不分叉。

event/outbox 1:1。

export 明確採 at-least-once 或有 dedupe。

文件說明 hash chain 的限制。

off-host anchor 列為 production gate。

API 標示 durable／legacy。

有 machine-readable adoption catalog。

migrated path 不再直接寫 legacy JSONL。

idle-host suite 0 failed。

進度文件標示 partial adoption，而非全面完成。

六、建議狀態文字

Schema v2 durable audit ledger and export outbox foundation is implemented for selected UoW execution paths. Audit adoption remains partial: legacy domain mutations still use best-effort JSONL and are not transactionally durable. JSONL is an asynchronous projection for migrated DB events. Full-domain migration, external chain anchoring, and idle-host full-suite confirmation remain open.
