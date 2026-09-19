---
name: approval-boundary
description: Use for approval kinds, request/approve/apply flows, auth/authz, agent or MCP mutation paths, auto-approval, human confirmation, promotion gates, or mutation audit behavior.
---

# Approval Boundary

This skill owns generic approval/auth/mutation-gate mechanics.

## Rules

- Material mutations use reviewed request → approval → apply semantics unless a
  canonical invariant names an exception.
- Reject invalid/dangerous input at request time and revalidate stale/target
  state at approval time.
- Agent/LLM/MCP channels may query and create proposals; they do not gain
  approve/reject/shell/exec authority.
- Provider choice never changes approval authority.
- New approval kinds, auto-approval exceptions, authorization semantics, or
  bypasses require a named decision.
- Preserve requester/decider authorization and audit behavior.
- Do not duplicate generic approval mechanics into domain skills.

## Validation

Run focused approval/auth/agent-tool tests. For changed approval semantics cover
request creation, invalid input, stale-state revalidation, authorization, and
audit. Any failed invariant blocks completion.
