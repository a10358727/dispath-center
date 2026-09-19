# Technical Reference

本目錄保存**相對穩定、需要長期查閱的技術參考**。這些文件說明「系統怎麼組成／怎麼設定」，不是 roadmap，也不是 decision authority。

| 文件 | 用途 |
|---|---|
| [`API_ROUTING.md`](./API_ROUTING.md) | API router / legacy-v2 路由與邊界 |
| [`AUDIT_LEDGER.md`](./AUDIT_LEDGER.md) | Audit event / ledger 契約與維護 |
| [`MIGRATIONS.md`](./MIGRATIONS.md) | SQLite schema migration 規則與歷史 |
| [`PACKAGING.md`](./PACKAGING.md) | 套件、build、distribution 邊界 |
| [`REPOSITORIES.md`](./REPOSITORIES.md) | Persistence repository / UoW 邊界 |
| [`SETTINGS.md`](./SETTINGS.md) | runtime settings / env / feature switches |
| [`COVERAGE_BASELINE.md`](./COVERAGE_BASELINE.md) | 測試 coverage baseline |
| [`TYPECHECK_BASELINE.md`](./TYPECHECK_BASELINE.md) | type-check baseline |

## 放置原則

- 會隨操作步驟變動的內容 → `../runbooks/`
- 架構裁定／安全規則 → `../PLATFORM_CHARTER.md` 或 `../DECISIONS.md`
- capability 現況 → `../CAPABILITY_LEDGER.md`
- 已過期技術計畫 → `../archive/`
