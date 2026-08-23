# DG-CONVERSATION-V1 — 專案內 AI 對話（AIConversation）決策閘

> Status: **awaiting explicit human decision; nothing implemented.**
> 本文件是 review packet，不是核准紀錄，也不授權任何 invariant 變更。
> 裁定後請把結論記入 `docs/DECISIONS.md`（權威紀錄）。
> 背景裁定：PROD-4（2026-08-23，每 Project 長期對話 + 受控 task 並存；
> 第一版單一 main conversation；對話本身無執行權）；同日方向選擇：
> 對話腦採 **control-plane orchestrator**（不採 runner 常駐互動 session）。

## 1. 目的與範圍

給每個 Project 一個 ChatGPT/Claude 式的持續對話視窗，包覆在該專案脈絡下：
看得見專案的版本／任務／Run／結果，能以自然語言指揮開發與實驗——但每個
實際動作仍是獨立的 controlled task + approval。第一版 = 每專案單一 main
conversation，訊息持久化於 SQLite。

不在範圍（明確 non-goals）：AgentSession／provider 常駐互動 session、
多 conversation、跨專案脈絡、自動迭代（PROD-5 另案）、任何新 approval
kind、任何 invariant 變更、streaming 架構重寫。

## 2. 現況（已驗證的接縫）

- `app/llm.py`：Anthropic 隔離層（`DEFAULT_MODEL="claude-sonnet-5"`，
  key 缺席即降級，INV-LLM-5）。
- `app/agent_runtime.py` + `app/agent_tools.py`：JSON tool loop 與
  allowlist 工具表（唯讀 + `request_*` 建卡；INV-LLM-1/2/3 測試釘住；
  最壞情況血本 = 一張待審卡）。
- `app/chat.py` + WS `/ws` + `POST /agent/chat`：既有全域聊天通道。
- 紅線（development-platform.md §7）：browser/WS/in-memory 不得成為任何
  Development Plane 物件的唯一真相 → 訊息必須落 DB。

## 3. 建議契約（裁定點 CV-1…CV-6）

- **CV-1 Domain 與真相**：additive migration 新增 `ai_conversations`
  （每 Project 唯一 main，`project_id` UNIQUE）與
  `ai_conversation_messages`（role、bounded content、綁定的
  approval/task/run 參照、created_at）。SQLite 是唯一真相；WS 只是傳輸。
  訊息大小與單次載入筆數有上限；retention/刪除另案裁定。
- **CV-2 對話腦與邊界**：沿用既有 LLM tool loop（Anthropic API）。
  工具集 = 既有 allowlist **原樣**，僅注入 project 脈絡；不新增任何
  工具權限；INV-LLM-1/2/3 一字不動。Key 缺席時 conversation 介面明確
  降級（顯示未設定，不拋錯）。
- **CV-3 Project 綁定**：conversation 釘死一個 project；tool loop 的
  查詢與 `request_*` 提案預設以該 project 為 scope；回應中引用的
  task/run id 持久化為訊息參照（可點跳轉）。
- **CV-4 Task-chaining 語意**：對話可提案 engineering task／Run；每個
  提案各自成卡、各自核准；worktree 生命週期維持現行 per-task 模式
  （RemoteWorkspace 跨任務持久化屬未來另案）。對話永不因「上一個任務
  成功」自動建立下一個動作。
- **CV-5 UI**：Project 詳情頁新增「AI Engineer」分頁：訊息列表（DB 載入）
  + 輸入框 + 送出；v1 沿用既有 chat 傳輸與非串流回覆模式，textContent
  渲染、loading/error/降級狀態。全域 chat 分頁保留不動。
- **CV-6 旗標**：`PROJECT_CONVERSATION_V1_ENABLED=false` 預設關閉；
  關閉時路由與 UI 分頁隱藏，資料保留。

## 4. 驗收標準（bounded slice 的 Done）

1. Migration 測試（SCHEMA+雙軌，INV-STATE-3）；每 Project 唯一 main
   conversation 由 DB 約束保證。
2. 假 LLM client 全流程：送訊息 → tool loop → 回覆與訊息落 DB →
   重啟後歷史完整重現（DB 唯一真相測試）。
3. 提案流：對話中請求改碼 → 產生 pending approval → 核准後任務照現行
   生命週期執行 → 對話訊息帶 task 參照。
4. 邊界回歸：forbidden-modules/forbidden-names 釘住斷言原樣通過；
   工具表零擴張；prompt injection 最壞情況仍是一張待審卡。
5. 旗標關閉：路由 404／UI 隱藏、既有 chat 與全部既有測試零變化。
6. Key 缺席：介面顯示降級訊息，核心功能不受影響（INV-LLM-5）。

## 5. Implementation packet（裁定後交 sonnet-coder）

| 項 | 內容 |
|---|---|
| 檔案 | migration（`app/migrations.py`+`app/db.py`）、`app/conversations.py`（新，domain + 存取）、`app/main.py`（project-scoped conversation 路由）、`app/agent_runtime.py`（project 脈絡注入，不動工具表）、`static/`（AI Engineer 分頁）、`app/config.py`/`settings/features.py`（旗標） |
| 測試 | 新 `tests/test_project_conversation.py`（§4 全項，假 LLM client）、前端靜態斷言、計數閘依既定流程 |
| 邊界 | INV-LLM-*／approval-boundary 全套；不新增工具、不新增 approval kind、不動 `/ws` 既有行為 |
| BLOCKED 條件 | 需要新工具權限、新 approval kind、或 streaming 重寫時停工回報 |

## 6. 裁定模板

```text
DG-CONVERSATION-V1:
CV-1 domain 與 DB 唯一真相: approve / 修改
CV-2 沿用既有 tool loop、工具零擴張: approve / 修改
CV-3 project 綁定與參照: approve / 修改
CV-4 per-task chaining、不自動連鎖: approve / 修改
CV-5 UI（AI Engineer 分頁、非串流 v1）: approve / 修改
CV-6 旗標 PROJECT_CONVERSATION_V1_ENABLED=false: approve / 修改
整體: approve bounded implementation / defer
```
