---
name: approval-boundary
description: Protect approval, auth/authorization, LLM/MCP mutation paths, auto-approval, Development Agent mutation, and ProjectVersion promotion boundaries.
---

# Approval Boundary

The approval flow is the gate for material mutation. Agents never gain decision or execution authority.

## Read only what applies

- `references/approval-contract.md` — operational approval/auth/audit checklist.
- Relevant `INV-APPROVAL-*`, `INV-LLM-*`, `INV-AUDIT-*`, `INV-TEST-2` in `../dispatcher-domain/references/invariants.md`.
- `docs/DECISIONS.md` for named rulings and `docs/CAPABILITY_LEDGER.md` when rollout matters.
- `development-agent-safety` when a Development Agent/workspace/promotion path is involved.

## Hard boundary

- Material mutations use reviewed request → approval → apply paths unless an invariant explicitly says otherwise.
- Reject invalid input at request time and revalidate stale/target state at approval time.
- No agent tool set gains approve/reject/shell/exec/run-command authority.
- New approval kinds, exceptions, or auto-approval behavior require explicit reviewed semantics.

## Validation

Run only the focused approval/auth/agent-tool tests for the touched path. Any failed invariant blocks completion. Never contact real services/SSH or runtime data.
