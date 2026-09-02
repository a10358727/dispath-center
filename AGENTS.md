# Agent entry point｜非 Claude agent 入口

Dispatch Center 是 **Agent-native Engineering Platform**：AI 負責思考與提案，平台負責治理與執行，人負責決定。
任何在這個 repository 裡工作的 coding agent（Codex、Claude Code 或其他）都適用下列規則：

1. 先讀 `docs/PLATFORM_CHARTER.md`（§4 架構模型、§6 不變式、§7 裁定登錄）與 `CLAUDE.md` 的 Global rules；
   能力現況以 `docs/CAPABILITY_LEDGER.md` 為準，路線圖（`docs/product/ROADMAP.md`）只是方向。
2. 改任何 `INV-*` 或新增能力類別（approval kind、生命週期狀態、provider、validation mechanism、
   硬體工作類型）都需要使用者具名裁定，記入 `docs/DECISIONS.md`；不得在實作中順手更動。
   既有裁定範圍內的 bounded packet 以 10 行「補充紀錄」記錄（模板在 `docs/DECISIONS.md`
   DG-CONSOLIDATION-v1 C-6），文件更新範圍以 `CLAUDE.md` 的 checklist 為準。
3. 只在自己被指派的範圍內改檔；不弱化任何邊界測試；測試一律用假介面，不碰真機、真憑證、
   runtime `jobqueue.db`／`audit.jsonl`／`servers.yaml`。
4. Agent 永遠不核准任何請求、不取得 shell／SSH／憑證；能力上限是「產出可審閱的 diff 與待核准的提案」。
5. 一次只有一個寫入型 agent 處理一個任務；完成後回報：狀態、改了什麼、驗證證據、剩餘風險。

Claude Code 專用的 skill 路由與驗證政策在 `CLAUDE.md` 與 `.claude/skills/`。
