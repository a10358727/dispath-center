---
name: development-agent-safety
description: Protect the Development Agent boundary for Codex, Claude Code, future providers, workspaces, agent sessions, validation, diffs, engineering tasks, and ProjectVersion promotion.
---

# Development Agent Safety

A Development Agent is a bounded Development Plane collaborator, never an unrestricted shell/SSH/root agent. Provider choice never changes authority.

## Read only what applies

- `references/boundary-details.md` — provider neutrality, selection, workspace, approval, validation/execution details.
- `../dispatcher-domain/references/development-platform.md` §4/§5 — plane and agent model.
- Relevant `INV-LLM-*`, `INV-APPROVAL-*`, `INV-SSH-*` in `../dispatcher-domain/references/invariants.md`.
- `docs/DECISIONS.md` / `docs/CAPABILITY_LEDGER.md` when capability semantics or rollout matters.

## Hard boundary

- Work only inside dispatch-created isolated workspaces/worktrees.
- Development validation is bounded workspace test/lint/typecheck/build; Compute execution always re-enters governed platform execution.
- Agents never approve/reject, receive unrestricted shell/exec/SSH/credentials, mutate runtime state, promote/deploy, push external origins, or bypass authorization/approval/promotion boundaries.
- Verify provider/session/validation capabilities from current code + tests before relying on them; do not invent lifecycle semantics.

## Validation

Run only the focused `tests/test_coding_*`, `tests/test_engineering_*`, `tests/test_agent_*`, provider/session tests, and boundary tests relevant to the change. Never contact real providers/workers or runtime data.
