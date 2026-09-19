# Dispatch Center Documentation

這裡是整個專案的**文件總入口**。如果不知道某件事應該去哪裡查，先從這一頁開始。

> 核心原則：**治理真相、實作真相、能力現況、產品方向、歷史資料要分開。**
> 文件彼此衝突時，不要用「比較新看起來的文件」猜答案，依下方 Truth Order 判定。

## Truth Order｜真相順序

1. **安全／架構真相**：[`PLATFORM_CHARTER.md`](./PLATFORM_CHARTER.md) §6 invariants + [`DECISIONS.md`](./DECISIONS.md) 具名裁定
2. **實作真相**：current code + tests
3. **能力現況**：[`CAPABILITY_LEDGER.md`](./CAPABILITY_LEDGER.md)
4. **產品方向**：[`product/ROADMAP.md`](./product/ROADMAP.md)
5. **歷史資料**：[`archive/`](./archive/)

## Start Here｜依目的找文件

| 你要做什麼 | 先看 | 再看 |
|---|---|---|
| 理解產品是什麼、邊界在哪 | [Platform Charter](./PLATFORM_CHARTER.md) | [V0.1 Architecture](./product/V0_1_PRODUCT_ARCHITECTURE.md)、[Roadmap](./product/ROADMAP.md) |
| 確認某能力目前真的做到哪 | [Capability Ledger](./CAPABILITY_LEDGER.md) | code + tests |
| 確認某個架構決定為什麼這樣做 | [Decision Log](./DECISIONS.md) | [Decision Packets](./decisions/) |
| 開發新功能／修改 protected boundary | [Platform Charter](./PLATFORM_CHARTER.md) | [Decision Log](./DECISIONS.md)、[`AGENTS.md`](../AGENTS.md) / [`CLAUDE.md`](../CLAUDE.md) |
| 部署、驗收、canary、故障處理 | [Runbooks](./runbooks/) | [Reference](./reference/) |
| 查設定、migration、API routing、audit | [Reference](./reference/) | code + tests |
| 查 canary / restore / acceptance 證據 | [Evidence](./evidence/) | Capability Ledger |
| 看過去怎麼演進 | [Archive](./archive/) | Decision Packets |

## Canonical Documents｜三份核心文件

### 1. PLATFORM_CHARTER.md
定義產品定位、兩個 Plane、non-goals、核心 lifecycle、全部 `INV-*` 不變式與 decision registry。

**只有具名裁定才能改 invariant 或新增新的能力類別。**

### 2. DECISIONS.md
append-only 的**權威裁定紀錄**。這裡回答「使用者最後到底批准了什麼」。

`docs/decisions/` 裡的文件是 decision packet / provenance，不等於裁定本身。

### 3. CAPABILITY_LEDGER.md
目前 capability 的狀態表。回答「implemented / default / pilot / canary 到哪」。

Ledger 不得自行授權新能力，也不能改變 Charter 或 Decision 的語意。

## Directory Map｜目錄

```text
docs/
├── README.md                 ← 你現在看的總入口
├── PLATFORM_CHARTER.md       ← 架構 / 安全 / invariants
├── DECISIONS.md              ← 權威裁定紀錄
├── CAPABILITY_LEDGER.md      ← 能力現況
│
├── product/
│   ├── README.md
│   └── ROADMAP.md            ← 未來產品方向
│
├── decisions/
│   ├── README.md
│   └── DG_*.md               ← decision packet / provenance
│
├── reference/
│   ├── README.md
│   └── *.md                  ← 長期技術參考
│
├── runbooks/
│   ├── README.md
│   └── *.md                  ← 實際操作 / rollout / acceptance
│
├── evidence/
│   ├── README.md
│   └── *                     ← 真實 canary / restore evidence
│
├── examples/
│   ├── README.md
│   └── *.example.*           ← 範例輸入 / evidence template
│
└── archive/
    ├── README.md
    └── *.md                  ← 已被取代的歷史文件
```

## Document Types｜文件放置規則

### Governance
只能放根目錄三份 canonical 文件：Charter、Decision Log、Capability Ledger。不要新增第四份「現在狀態總結」來跟它們競爭。

### Product
只描述**產品想成為什麼**，不描述 capability 現況。現況一律回 Ledger。

### Decision Packet
保存某次 decision 的完整背景、選項、contract、review provenance。是否已批准以 `DECISIONS.md` 為準，不以檔名中的 `DRAFT` / `DECISION` 猜測。

### Reference
描述相對穩定、可長期查閱的技術規格，例如設定、migration、API routing、audit ledger、packaging。

### Runbook
描述「照著做」的操作程序；必須能清楚說明前提、步驟、成功條件與 rollback / stop condition。

### Evidence
保存真實執行產生的驗證證據。Evidence 不是規格，也不反過來改變 contract。

### Archive
只放已被 canonical 文件取代、且不再是 live authority 的歷史計畫／快照。不要從 archive 推論現況。

## Maintenance Rules｜維護規則

1. 新 capability class / invariant change：先 decision，再 implementation。
2. 一般 bug fix / refactor：不要順手改 Charter / Decision。
3. Capability 狀態改變：更新 Ledger 對應 row，不新增新的「進度總結」文件。
4. 新操作流程：放 `runbooks/`；新長期技術說明：放 `reference/`。
5. 歷史計畫完成後，如果已被 canonical 文件取代且沒有 live reference，再移入 `archive/`。
6. 文件連結優先使用相對路徑；不要複製同一份規則到多個地方維護。
7. 若文件與 code/tests 衝突，依 Truth Order 判定並修正較低層級來源。

## AI / Coding Agent Entry Points

- 非 Claude coding agent：先讀 [`AGENTS.md`](../AGENTS.md)
- Claude Code：先讀 [`CLAUDE.md`](../CLAUDE.md)
- 架構／跨 Plane 設計：再讀 [`PLATFORM_CHARTER.md`](./PLATFORM_CHARTER.md)
- 不要預設掃完全部 `docs/`；只讀與當前工作有關的文件。
