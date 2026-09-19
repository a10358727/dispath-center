# Product Documents

本目錄回答兩件事：

1. **Dispatch Center 最後要成為什麼產品？**
2. **目前 V0.1 要先完成哪一條產品／系統閉環？**

## Current documents

- [`V0_1_PRODUCT_ARCHITECTURE.md`](./V0_1_PRODUCT_ARCHITECTURE.md) — **V0.1 產品與系統架構規格**；定義目標、邊界與 Definition of Done。
- [`V0_1_IMPLEMENTATION_PLAN.md`](./V0_1_IMPLEMENTATION_PLAN.md) — **V0.1 唯一實作計畫與進度帳本**；work packet、狀態、阻塞、驗證與下一步都更新在這裡。
- [`ROADMAP.md`](./ROADMAP.md) — 長期產品路線圖、最終體驗與里程碑。

## How to use them

`V0_1_PRODUCT_ARCHITECTURE.md` 是目前近期實作的 target specification；`V0_1_IMPLEMENTATION_PLAN.md` 則負責追蹤實作進度。完成任何 planned work packet 時，必須在同一個變更中更新 Implementation Plan。

兩者都**不是治理 authority**。若目標碰到新的 capability class、approval kind、lifecycle state、provider、validation mechanism 或 `INV-*`，仍必須先依既有 decision-gate 流程裁定。

`ROADMAP.md` 描述更長期的產品方向，不代表每一項已核准或已實作。

## Boundary

本目錄**不是現況帳本**。實際 capability 狀態仍以：

1. [`../PLATFORM_CHARTER.md`](../PLATFORM_CHARTER.md) + [`../DECISIONS.md`](../DECISIONS.md)
2. current code + tests
3. [`../CAPABILITY_LEDGER.md`](../CAPABILITY_LEDGER.md)

為準。

已完成或被取代的 product plan 應移到 [`../archive/`](../archive/)，避免出現多套 active plan。
