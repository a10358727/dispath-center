---
name: approval-boundary
description: Protect the dispatch-center approval and LLM/MCP security boundary. Use when changing approvals, auto-approval, mutating APIs, auth/authorization, LLM/MCP tools, development-agent tools (Codex, Claude Code, or any provider), chat, ProjectVersion promotion, or workspace promotion behavior.
---

# Approval Boundary

The approval flow is the single gate for material mutation in **both** planes.
Development Plane work (development agents, workspaces, promotion) gets no
lighter gate because it "only touches code", and no agent provider gets a
lighter gate because of what it is.

## Required reads (canonical semantics live there, not here)

- `../dispatcher-domain/references/invariants.md` — the relevant
  `INV-APPROVAL-*`, `INV-LLM-*`, `INV-AUDIT-*`, `INV-TEST-2` sections. Do not
  restate, re-derive, or paraphrase their conditions when implementing; read
  the actual text.
- `docs/DECISIONS.md` — named rulings, especially DG-CODE-PROMOTE-v1
  (promotion is human-only, P-1…P-5) and DG-SELF-APPROVAL-OPTION-v1
  (default-off high-risk self-approval, humans only).

## Hard boundary

- Every material mutation maps to an approval kind in `VALID_APPROVAL_KINDS`,
  created by `request_*`, landed only by `approve()`; the only exceptions are
  those the invariants explicitly enumerate (`INV-APPROVAL-1`). A new
  exception is a user decision.
- Dangerous/invalid input is rejected at request creation; target state is
  revalidated at approval time (`INV-APPROVAL-2/3`).
- Auto-approval stays exactly `enqueue|stop` (`INV-APPROVAL-4`); the only
  policy-scoped automatic decision is `auto_placement` under
  `INV-APPROVAL-4b`, as a separate mechanism.
- No agent tool set ever gains approve/reject/shell/exec/run_command, and no
  LLM-to-execution path exists (`INV-LLM-1/2/3`). An agent channel's ceiling
  is one pending approval card.
- Auth is default-on with a closed exemption set (`INV-APPROVAL-5`); every
  lifecycle action is audited (`INV-AUDIT-2`).
- Development Plane triggers: coding/engineering approval kinds, ProjectVersion
  promotion, and `project_instance_update_v2` all live behind this gate with
  the semantics recorded in `docs/DECISIONS.md`. Agent/provider selection
  (manual or Auto) never widens the approval surface — no new provider brings
  a new auto-approval path or tool. Boundary details:
  `development-agent-safety`.

## Validation

Normally: `pytest tests/test_approvals.py tests/test_autoapprove.py
tests/test_agent_tools.py tests/test_mcp_bridge.py -q`

For any new/changed kind add tests for: creation, dangerous-input rejection at
request time, stale-state rejection at approve time, and audit content. Never
weaken pinned boundary tests (`INV-TEST-2`).

Do not contact real services or SSH hosts, and do not modify runtime data or
config. Any failed invariant blocks completion.
