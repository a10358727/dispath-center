# Product Documents

本目錄回答兩件事：

1. **Dispatch Center 最後要成為什麼產品？**
2. **目前 V0.1 要先完成哪一條產品／系統閉環？**

## Current documents

- [`V0_1_PRODUCT_ARCHITECTURE.md`](./V0_1_PRODUCT_ARCHITECTURE.md) — **V0.1 產品與系統架構規格**；供 Codex / Claude Code 制定實作計畫與拆 work packets。
- [`ROADMAP.md`](./ROADMAP.md) — 長期產品路線圖、最終體驗與里程碑。

## How to use them

`V0_1_PRODUCT_ARCHITECTURE.md` 是目前近期實作的 target specification，但**不是治理 authority**。若其中任何目標碰到新的 capability class、approval kind、lifecycle state、provider、validation mechanism 或 `INV-*`，仍必須先依既有 decision-gate 流程裁定。

`ROADMAP.md` 描述更長期的產品方向，不代表每一項已核准或已實作。

## Boundary

本目錄**不是現況帳本**。實際 capability 狀態仍以：

1. [`../PLATFORM_CHARTER.md`](../PLATFORM_CHARTER.md) + [`../DECISIONS.md`](../DECISIONS.md)
2. current code + tests
3. [`../CAPABILITY_LEDGER.md`](../CAPABILITY_LEDGER.md)

為準。

已完成或被取代的 product plan 應移到 [`../archive/`](../archive/)，避免出現多套 active plan。
