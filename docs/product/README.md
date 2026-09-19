# Product Documents

本目錄回答：

1. **V0.1 要做成什麼？**
2. **目前做到哪裡、下一步是什麼？**
3. **長期要往哪裡走？**

## Current documents

- [`V0_1_PRODUCT_ARCHITECTURE.md`](./V0_1_PRODUCT_ARCHITECTURE.md) — V0.1 目標、架構、邊界與 Definition of Done。
- [`V0_1_UX_PLAN.md`](./V0_1_UX_PLAN.md) — V0.1 使用者介面、AI Workspace、Context、Overview、Compute 與互動狀態規格。
- [`V0_1_IMPLEMENTATION_PLAN.md`](./V0_1_IMPLEMENTATION_PLAN.md) — **V0.1 唯一實作計畫與進度帳本**；work packet、阻塞、驗證與下一步都更新在這裡。
- [`ROADMAP.md`](./ROADMAP.md) — 長期產品方向與里程碑。

## Rules

Architecture 定義「系統要成為什麼」；UX Plan 定義「使用者怎麼操作」；Implementation Plan 記錄「現在做到哪裡」。

完成任何 planned work packet 時，必須在同一個變更中更新
`V0_1_IMPLEMENTATION_PLAN.md`。

這些 product 文件都不是治理 authority。若工作涉及新的 capability class、
approval kind、lifecycle state、provider、validation mechanism 或 `INV-*`，
仍必須依現有 decision-gate 流程裁定。

實際能力現況仍以：

1. [`../PLATFORM_CHARTER.md`](../PLATFORM_CHARTER.md) + [`../DECISIONS.md`](../DECISIONS.md)
2. current code + tests
3. [`../CAPABILITY_LEDGER.md`](../CAPABILITY_LEDGER.md)

為準。
