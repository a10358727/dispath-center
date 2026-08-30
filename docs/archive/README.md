# Archived documents

本目錄存放**已過期、且沒有任何 live reference(code/tests/scripts/skills)**
的歷史文件。它們只是 evidence,不是 current authority;現況一律以
`docs/CAPABILITY_LEDGER.md`、`docs/DECISIONS.md` 與 current code + tests 為準。

| 檔案 | 原路徑 | 歸檔日期 | 取代來源 |
|---|---|---|---|
| `AGENT_SESSION_V1_PLAN.md` | `docs/product/` | 2026-08-30 | `docs/product/ROADMAP.md`、`docs/DECISIONS.md` |
| `CURRENT_STATE.md` | `docs/` | 2026-08-30 | `docs/CAPABILITY_LEDGER.md`、`docs/DECISIONS.md` |
| `DISPATCH_CENTER_CURRENT_SYSTEM_AND_IMPROVEMENT_PLAN.md` | `docs/` | 2026-08-30 | `docs/PLATFORM_CHARTER.md`、`docs/CAPABILITY_LEDGER.md` |
| `FULL_PLATFORM_SECOND_PASS_PLAN.md` | `docs/product/` | 2026-08-30 | `docs/product/ROADMAP.md`、`docs/DECISIONS.md` |
| `GOAL_1_IMPLEMENTATION_PLAN.md` | `docs/` | 2026-08-25 | `docs/CAPABILITY_LEDGER.md`(`oidc_identity`/`authorization_*` rows)、`docs/DECISIONS.md` |
| `GOAL_2_AUTOMATED_DISPATCH_PLAN.md` | `docs/` | 2026-08-30 | `docs/CAPABILITY_LEDGER.md`、`docs/DECISIONS.md` |
| `GOAL_3_COMPLETION_STATUS.md` | `docs/` | 2026-08-30 | `docs/CAPABILITY_LEDGER.md`、`docs/DECISIONS.md` |
| `GOAL_3_FUTURE_WORK_PLAN.md` | `docs/` | 2026-08-30 | `docs/CAPABILITY_LEDGER.md`、`docs/DECISIONS.md` |
| `IMPLEMENTATION_PROGRESS.md` | `docs/` | 2026-08-30 | `docs/CAPABILITY_LEDGER.md`、`docs/evidence/` |
| `NEXT_IMPLEMENTATION_PLAN.md` | `docs/` | 2026-08-30 | `docs/CAPABILITY_LEDGER.md`、`docs/product/ROADMAP.md` |
| `PERSONAL_PILOT_PLAN.md` | `docs/product/` | 2026-08-30 | `docs/product/ROADMAP.md`、`docs/DECISIONS.md` |
| `PRODUCT_V2_EXECUTION_PLAN.md` | `docs/`(原 `DISPATCH_CENTER_PRODUCT_V2_EXECUTION_PLAN.md`) | 2026-08-30 | `docs/PLATFORM_CHARTER.md`、`docs/DECISIONS.md`。根目錄 `PLAN.md` 曾是本檔的位元完全相同副本,已一併移除。 |
| `STAGE_ACCEPTANCE_MANUAL.md` | `docs/` | 2026-08-30 | `docs/PLATFORM_CHARTER.md`、`docs/CAPABILITY_LEDGER.md` |
| `TESTING_REPORT.md`(2026-08-06 快照) | `docs/` | 2026-08-25 | `docs/archive/IMPLEMENTATION_PROGRESS.md`、`docs/CAPABILITY_LEDGER.md` |

歸檔準則(缺一不可):

1. 內容是狀態快照或已完成的歷史計畫,且已被 canonical 來源取代;
2. 沒有任何 code / tests / scripts / CI / `.claude` skills 的引用;
3. 沒有被 `tests/test_document_authority.py` 釘住路徑。

注意:`docs/decisions/DG_C_INVARIANT_REVISION_DRAFT.md` 同樣是歷史文件,
但被 `tests/test_document_authority.py` 釘在 `docs/decisions/` 並以
`superseded_by:` 標頭標示——它**留在 `docs/decisions/`**,不移入本目錄。
`DG_*_DECISION.md` 決策 packet 是 `docs/DECISIONS.md` 裁定的 provenance,
一律留在 `docs/decisions/`。
