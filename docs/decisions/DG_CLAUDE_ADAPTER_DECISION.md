# DG-CLAUDE-ADAPTER — Claude Code AgentProvider adapter 決策閘

> Status: **approved on 2026-08-24 — approve bounded implementation**
> (C-1…C-6 全部採建議值,另納入 Web UI provider selector 與 verification
> 條件;權威裁定紀錄見 `docs/DECISIONS.md` 2026-08-24 條目)。
> 本文件是 review packet,不是核准紀錄,也不授權任何 invariant 變更。
> 背景裁定:PROD-7(2026-08-23,最終完成品以 Claude 為主力 provider)。

## 1. 目的與範圍

把 `claude-code-v1` provider adapter 以 **bounded implementation only** 的
方式納入 reviewed coding-agent registry——與 2026-07-16 D1
(`codex-app-server-v1`)完全相同的准入模式:實作 + 假協議測試,**不核准
正式啟用**;真實環境啟用需另一次獨立的 canary/rollback 簽核。

不在範圍(明確 non-goals):

- AIConversation / AgentSession domain(PROD-4 的長期對話,另案)
- Auto provider selection 引擎(另案)
- 任何新的 validation mechanism——本 slice **完全沿用**現行 Job-backed
  path(approved `type="coding"` Job + SSH/tmux/sentinel),不觸發
  development-platform.md 所述「新機制需具名裁定」條款
- 任何 approval kind 新增、auto-approval 變更、invariant 變更
- GitHub、streaming UI、Compute Plane 任何改動

## 2. 現況(已驗證的接縫)

- `app/coding_agents.py`:`_APPROVED_CODING_AGENT_PROVIDERS`(現含 codex)
  與 `_EXPERIMENTAL_UNWIRED_CODING_AGENT_PROVIDERS`(D1 的
  registered-but-unwired 模式,現含 codex-app-server-v1)。
- `app/approvals.py` 的 engineering task 生命週期**已帶**
  `agent_provider_id`(預設 `codex`),經 `require_coding_agent_provider()`
  驗證,launch/output contract 與 reviewed provider 不一致即 fail closed。
- `app/engineering_tasks.py`:`ENGINEERING_TASK_PROVIDER_ID` 目前固定為
  Codex;request 端尚未開放呼叫者指定 provider。

## 3. 建議契約(裁定點 C-1…C-6)

- **C-1 准入形態**:`claude-code-v1` 進入 registry。第一切片放在
  `_APPROVED_CODING_AGENT_PROVIDERS` 但由 default-off 設定鍵閘住可選性
  (見 C-4),或比照 app-server 放 experimental-unwired——**建議前者**,
  因為 exec 型一次性 turn 與 codex-exec-v1 同構,不涉及未審協議生命週期。
- **C-2 執行承載**:完全沿用現行 runner 模式——Claude Code CLI 安裝並登入
  在指定 runner 機(部署事實,同 `CODEX_RUNNER_SERVER` 慣例;建議沿用同
  一台 runner,新增 `CLAUDE_CODE_*` 設定鍵組),task script 由既有純函式
  確定性組裝,instruction 走檔案+stdin,永不進 shell 字串(INV-SSH-2)。
  headless 一次性 turn(`claude -p` 非互動模式)、獨立 worktree/branch、
  哨兵協議判終態、approved stop 停止——與 codex turn 逐條同規。
- **C-3 provider 選擇**:request payload 開放**顯式** `agent_provider_id`
  (預設維持 `codex`),僅接受 registry 內、且未被 C-4 閘門關閉的 id;
  Auto selection 不在本 slice。
- **C-4 旗標**:`CLAUDE_CODE_AGENT_V1=false` 預設關閉;關閉時
  `claude-code-v1` 不可被選取、不出現在可選清單。啟用是部署決策,
  不隨本裁定生效。
- **C-5 版本 pin 與 fail-closed**:pin 已審閱的 Claude Code CLI 版本範圍
  與輸出形狀;版本探測不符或輸出無法解析時 fail closed(task 失敗並留
  證據),永不 fallback 到其他 provider(development-platform.md §5b)。
- **C-6 credential**:Claude Code 的登入態只存在 runner 機(同 codex
  chatgpt auth 慣例);任何 API key/credential 永不進 instruction、
  prompt、DB、audit、diff。

## 4. 驗收標準(bounded slice 的 Done)

1. registry 列出 `claude-code-v1`,capability snapshot 誠實(無 app-server
   生命週期能力就標 false)。
2. `CLAUDE_CODE_AGENT_V1=false`(預設)時:選取該 id 的 request 被拒,
   既有 Codex 路徑零行為變化(全部既有測試綠)。
3. 開旗標後:`agent_provider_id="claude-code-v1"` 的 coding task 走完
   instruction → approval → worktree → 驗證 → diff → bundle 全流程
   (FakeSSH/假 runner,不碰真 CLI)。
4. 指令字串測試釘住:task script 為純函式產出、instruction 不進 shell。
5. unknown/disabled provider 拒絕測試;forbidden-modules/forbidden-names
   釘住斷言原樣通過(INV-TEST-2)。
6. 不新增 approval kind、不改 auto-approval、不改 invariants.md。

## 5. Implementation packet(裁定後交 sonnet-coder)

| 項 | 內容 |
|---|---|
| 檔案 | `app/coding_agents.py`(descriptor/capabilities/provider + registry)、`app/config.py`(`CLAUDE_CODE_AGENT_V1` 與 runner 設定鍵)、`app/engineering_tasks.py` / `app/approvals.py`(顯式 provider id 接受與旗標閘)、對應純函式 script builder |
| 測試 | `tests/test_coding_agents.py` 擴充、新 `tests/test_claude_code_agent.py`(旗標關/開、全流程 fake、指令字串、拒絕案例)、既有 engineering/coding 套件回歸 |
| 邊界 | development-agent-safety 全套;不碰 sshpool/localrun import 規則;不動 static_checks 釘住項 |
| BLOCKED 條件 | 發現需要新 approval kind、新 validation mechanism、或 registry 模式不敷使用時停工回報,不自行擴權 |

## 6. 裁定模板

```text
DG-CLAUDE-ADAPTER v1:
C-1 准入形態: approved-gated / experimental-unwired
C-2 執行承載: 沿用 Job-backed runner(建議)/ 其他(需另裁 validation mechanism)
C-3 顯式 provider 選擇: approve / defer
C-4 旗標名與預設關: approve / 修改
C-5 版本 pin + fail-closed: approve / 修改
C-6 credential 規則: approve / 修改
整體: approve bounded implementation / defer
```
