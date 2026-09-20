# Decision Packets

本目錄保存 **Decision Gate / Decision Packet 的完整背景與 provenance**。

> **權威裁定不在這裡。**
> 使用者最後批准／拒絕了什麼，以 [`../DECISIONS.md`](../DECISIONS.md) 為準；
> invariant 的現行文字以 [`../PLATFORM_CHARTER.md`](../PLATFORM_CHARTER.md) §6 為準；
> capability 現況以 [`../CAPABILITY_LEDGER.md`](../CAPABILITY_LEDGER.md) 為準。

## 最重要的閱讀規則

1. **檔名不是狀態。** 有些 `*_DRAFT.md` 後來已被正式核准；有些 `*_DECISION.md` 仍只是待裁定 packet。
2. 先看檔案開頭的 `Status`，再以 `../DECISIONS.md` 交叉確認。
3. Packet 保存「當時為什麼這樣決定」；不要用舊 packet 覆蓋後來的 Charter / Decision。
4. 已 superseded 的 packet 可以留在本目錄作 provenance，不代表它仍是 current contract。

## Awaiting / Not Approved

| 文件 | 狀態 | 用途 |
|---|---|---|
| [`AI_ENGINEERING_DECISION_GATE.md`](./AI_ENGINEERING_DECISION_GATE.md) | Awaiting explicit decisions | 舊 AI Engineering 尚未裁定的 decision gate；閱讀時先確認是否已被後續裁定取代 |
| [`DG_NODE_CANARY_DECISION.md`](./DG_NODE_CANARY_DECISION.md) | Draft / not approved | 真機 Node rollout gate；未填完整 rollout record 前不可核准 |
| [`DG_OPS_SLO_DECISION.md`](./DG_OPS_SLO_DECISION.md) | Draft / not approved | production-ready / SLO 證據門檻 |
| [`DG_SSH_HOSTKEY_DRAFT.md`](./DG_SSH_HOSTKEY_DRAFT.md) | Draft / not approved | Internet rental SSH host identity；`INV-SSH-8` gate，未裁定前不得改變 host-key 政策 |

## Approved / Historical Provenance

以下 packet 已有對應裁定，或其核心方向已被裁定。**實作狀態仍要回 Capability Ledger 查。**

| 文件 | 主題 |
|---|---|
| [`DG_AGENT_RUNTIME_V3_DECISION.md`](./DG_AGENT_RUNTIME_V3_DECISION.md) | Claude Agent SDK runner / Studio / Agent Runtime v3 |
| [`DG_AGENT_SESSION_CHECKPOINT_DECISION.md`](./DG_AGENT_SESSION_CHECKPOINT_DECISION.md) | AgentSession checkpoint → promotion bridge |
| [`DG_AMBIGUOUS_LAUNCH_DECISION.md`](./DG_AMBIGUOUS_LAUNCH_DECISION.md) | ambiguous SSH launch / durable attempt contract |
| [`DG_ASSISTANT_TOOLS_AND_AGENT_SESSION_V2_DRAFT.md`](./DG_ASSISTANT_TOOLS_AND_AGENT_SESSION_V2_DRAFT.md) | Assistant tools / AgentSession v2；雖名為 DRAFT，檔頭已標示已裁定 |
| [`DG_B4_DATASET_PREWARM_DRAFT.md`](./DG_B4_DATASET_PREWARM_DRAFT.md) | dataset prewarm；雖名為 DRAFT，已裁定並實作 |
| [`DG_C_INVARIANT_REVISION_DRAFT.md`](./DG_C_INVARIANT_REVISION_DRAFT.md) | INV-SSH / INV-NODE 歷史提案；現行文字已由 Charter supersede |
| [`DG_CLAUDE_ADAPTER_DECISION.md`](./DG_CLAUDE_ADAPTER_DECISION.md) | Claude provider adapter |
| [`DG_CODE_PROMOTE_DECISION.md`](./DG_CODE_PROMOTE_DECISION.md) | ProjectVersion / code promotion |
| [`DG_CONVERSATION_V1_DECISION.md`](./DG_CONVERSATION_V1_DECISION.md) | Conversation v1 歷史契約 |
| [`DG_DATASET_SNAPSHOT_DECISION.md`](./DG_DATASET_SNAPSHOT_DECISION.md) | immutable dataset snapshot |
| [`DG_EXEC_ATTEMPT_DECISION.md`](./DG_EXEC_ATTEMPT_DECISION.md) | durable execution attempt / outbox |
| [`DG_EXPERIMENT_V1_DECISION.md`](./DG_EXPERIMENT_V1_DECISION.md) | experiment matrix / one matrix one approval |
| [`DG_HARDWARE_EXECUTION_DRAFT.md`](./DG_HARDWARE_EXECUTION_DRAFT.md) | hardware execution；雖名為 DRAFT，檔頭已標示已核准 |
| [`DG_METRICS_CONTRACT_DECISION.md`](./DG_METRICS_CONTRACT_DECISION.md) | metrics-v1 contract |
| [`DG_NODE_V2_DECISION.md`](./DG_NODE_V2_DECISION.md) | Node v2 lease / recovery contract |
| [`DG_WP2D_CANARY_V2_DECISION.md`](./DG_WP2D_CANARY_V2_DECISION.md) | SSH attempt canary 8-hour window |

## Naming for New Packets

新文件建議使用：

- `DG_<TOPIC>_DRAFT.md`：尚未裁定
- 裁定後**不要求 rename**，避免破壞歷史 link；直接更新檔頭 Status，並在 `../DECISIONS.md` 留權威裁定紀錄。
- 不要因為一個 packet 被批准，就把它當成 capability 已啟用；啟用狀態仍回 Ledger。

回到文件總入口：[`../README.md`](../README.md)。
