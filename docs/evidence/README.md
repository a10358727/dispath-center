# Evidence

本目錄保存**真實環境產生的驗證證據**。Evidence 用來證明一個既有 contract 是否通過，不是用來定義 contract。

## Current evidence

- [`WP2D_V2_20260802_D73A38E.md`](./WP2D_V2_20260802_D73A38E.md) — WP-2D SSH attempt canary 歷史證據
- [`LOCAL_RESTORE_DRILL_20260806_AB0376F.json`](./LOCAL_RESTORE_DRILL_20260806_AB0376F.json) — local restore drill 證據

## Rules

- Capability 的 Canary / Evidence 狀態以 [`../CAPABILITY_LEDGER.md`](../CAPABILITY_LEDGER.md) 為準。
- 新 evidence 必須能追到 exact candidate / environment / contract version。
- 歷史 evidence 可以保留，但不能拿舊 candidate 的證據直接宣稱新 candidate 已通過。
- 範例格式放在 [`../examples/`](../examples/)，不要把 example 當真實 evidence。
