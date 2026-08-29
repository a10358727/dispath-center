# Approval contract

Canonical semantics remain in `../../dispatcher-domain/references/invariants.md` and `docs/DECISIONS.md`. This file is an operational checklist; read only the relevant sections.

## Mutation gate

- Material mutations map to a reviewed approval kind in `VALID_APPROVAL_KINDS`, are requested through `request_*`, and land only through `approve()` unless an invariant explicitly names an exception.
- Reject dangerous/invalid input at request creation; revalidate target/stale state at approval time.
- Auto-approval remains exactly the invariant-approved allowlist; `auto_placement` is a separate policy-scoped mechanism.
- New approval kinds, exceptions, or auto-approval paths require a named decision/invariant change.

## Agent boundary

- Agent/LLM/MCP channels may query and create pending proposals only; they never receive approve/reject/shell/exec/run-command authority.
- Provider selection never widens approval scope.
- Development Agent/promotion semantics follow `development-agent-safety` and the relevant named decisions.

## Auth/audit

- Preserve default-on authentication and the reviewed exemption set.
- Preserve authorization scope and requester/decider rules.
- Audit every lifecycle action without allowing audit failure to corrupt domain state.

## Validation

Run the focused approval/auth/agent-tool tests that own the touched path. For new/changed approval kinds cover request creation, dangerous-input rejection, stale-state rejection at approval time, authorization, and audit content. Never weaken pinned boundary tests.
