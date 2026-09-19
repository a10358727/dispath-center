---
name: development-agent-safety
description: Use for product Development Agent boundaries, AgentSession/workspace behavior, provider adapters, validation, diffs, checkpoints, ProjectVersion promotion, or Agent tool authority. Not for ordinary Codex local-shell permissions.
---

# Development Agent Safety

This is about the **product Development Agent**, not the coding agent running
inside this repository.

## Rules

- Development Agents operate only in dispatch-created isolated workspaces.
- Provider selection never widens authority.
- Development validation is bounded workspace test/lint/typecheck/build.
- Compute workloads re-enter the governed Compute Plane through a promoted
  ProjectVersion and the existing Run/ExecutionPlan path.
- Development Agents never approve/reject, receive unrestricted SSH/shell/
  credentials, mutate runtime state, self-promote/deploy, or bypass approval.
- Provider-specific CLI/runtime details stay inside provider adapters, not core
  domain contracts.
- Verify current provider/session capabilities from code/tests before relying on them.

`approval-boundary` owns generic approval mechanics.
`ssh-dispatch-safety` owns SSH execution mechanics.
`state-reconciliation` owns generic durable/recovery semantics.

## Validation

Run focused coding/engineering/agent/session/provider and boundary tests relevant
to the change. Never contact real providers/workers/runtime data.
