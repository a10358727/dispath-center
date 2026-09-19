# Runbooks

本目錄保存**實際操作程序**：部署、canary、驗收、rollout、故障與 recovery。

| 文件 | 用途 |
|---|---|
| [`RUNNER_AGENT_SETUP.md`](./RUNNER_AGENT_SETUP.md) | dispatch-agent runner 安裝與啟用 |
| [`PHASE5_NODE_CANARY_RUNBOOK.md`](./PHASE5_NODE_CANARY_RUNBOOK.md) | Node real-machine canary 程序 |
| [`WP_2D_CANARY_RUNBOOK.md`](./WP_2D_CANARY_RUNBOOK.md) | SSH attempt canary / WP-2D 驗證 |
| [`PHASE6_OPERATIONS_RUNBOOK.md`](./PHASE6_OPERATIONS_RUNBOOK.md) | operations / recovery 操作 |
| [`PR_03_OIDC_NONPROD_ACCEPTANCE.md`](./PR_03_OIDC_NONPROD_ACCEPTANCE.md) | OIDC non-production acceptance |

## Runbook 規則

一份 runbook 應該至少包含：

1. 前提與適用範圍
2. 要操作的確切環境／版本
3. 分步程序
4. 成功條件與需要保存的 evidence
5. stop / rollback 條件
6. 不得越過的安全 boundary

Runbook 只能執行已經被 Charter / Decision 授權的能力，不能自行創造新語意。
