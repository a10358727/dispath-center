---
name: approval-boundary
description: Protect the dispatch-center approval and LLM/MCP security boundary. Use when changing approvals, auto-approval, mutating APIs, auth/authorization, LLM/MCP tools, development-agent tools (Codex, Claude Code, or any provider), chat, ProjectVersion promotion, or workspace promotion behavior.
---

# Approval Boundary

The approval flow is the single gate for material mutation in **both** planes.
Development Plane work (development agents, workspaces, promotion) does not
get a lighter gate because it "only touches code", and no agent provider gets
a lighter gate because of what it is.

Read `../dispatcher-domain/references/invariants.md` sections `INV-APPROVAL-*`,
`INV-LLM-*`, `INV-AUDIT-*`, and `INV-TEST-2` as relevant.

## Required checks

- Every material state-changing action maps to an approval kind in
  `VALID_APPROVAL_KINDS`, is created by a `request_*` function, and lands only
  through `approve()`. The documented exceptions (read-only probes, low-risk
  `experiment_records` notes, idempotent hub-sync, and the closed
  authentication-bookkeeping list) are exactly those — a new exception is a
  user decision.
- Reject dangerous or invalid input **when the request is created** (400 +
  audit, no approval row), and revalidate the target's current state at
  approval time.
- Auto-approval stays exactly `enqueue` and `stop`. The only policy-scoped
  automatic decision is `auto_placement` under all of `INV-APPROVAL-4b`'s
  conditions; it is a separate mechanism, never a new entry in
  `maybe_auto_approve()`.
- Never add LLM/agent/MCP approve, reject, shell, exec or run_command tools,
  and never create a direct LLM-to-execution path. An LLM channel's ceiling is
  one pending approval card.
- Keep auth default-on: new endpoints are protected with zero configuration,
  and the exempt method/path set stays closed.
- Audit every lifecycle action; audit failure must not corrupt job state.

## Development Plane triggers

- **Development Agent (any provider — Codex today, Claude Code or others
  later)**: `coding_task`, `apply_patch`, `engineering_task_retry`,
  `engineering_task_discard` are approval-gated and never auto-approved.
  `engineering_task_pr` and `engineering_task_finalize` are deliberately
  absent and require a named ruling to exist. No agent ever decides its own
  request, and **agent/provider selection (manual or Auto) never widens the
  approval surface** — a new provider never brings a new auto-approval path or
  a wider tool set. See `development-agent-safety`.
- **ProjectVersion promotion**: `engineering_task_promote` is human-only under
  DG-CODE-PROMOTE-v1 P-1 — not via `maybe_auto_approve()` and not via the
  `INV-APPROVAL-4b` policy mechanism. Approve time re-verifies bundle bytes;
  promotion publishes to the local hub only, never GitHub.
- **Workspace promotion into the Compute Plane**: a Development Plane artifact
  reaches execution only as a promoted ProjectVersion. Never let a dirty
  worktree, an unpromoted bundle, or a `legacy_observed` version back a run.
- **Project instance update**: `project_instance_update_v2` promotes a checkout
  to a promoted revision; it stays outside the auto-approval allowlist and must
  refuse dirty/diverged/busy targets rather than forcing them clean.
- **Self-approval**: `ALLOW_HIGH_RISK_SELF_APPROVAL` (DG-SELF-APPROVAL-OPTION-v1)
  is default-off and only lets an enabled HUMAN who already holds decision
  authority decide their own high-risk approval. It never lets a Service actor
  decide, never widens roles or Project scope, and never removes the approval,
  immutable payload/digest, approve-time revalidation, idempotency, or durable
  audit. An LLM or Development Agent actor of any provider is never covered
  by it.

## Tests

Normally: `pytest tests/test_approvals.py tests/test_autoapprove.py
tests/test_agent_tools.py tests/test_mcp_bridge.py -q`

Add the matching kind-specific tests for any new approval kind: creation,
dangerous-input rejection, stale-state rejection at approve time, and audit
content.

Do not contact real services or SSH hosts, and do not modify runtime data or
config. Any failed invariant blocks completion.
