# SSH execution contract

Canonical semantics are in the relevant `INV-SSH-*` / `INV-STATE-*` sections of `docs/PLATFORM_CHARTER.md` §6 and named decisions.

## Command construction

- Keep remote command builders pure/testable; validate or quote every interpolated value.
- User/instruction text is data transferred through SFTP, never shell fragments.
- Read-only probes use a closed enumerated command set with secret filtering.
- Every SSH call has a timeout; long jobs detach through the reviewed tmux/sentinel mechanism.

## SSH-specific state consequences

Generic durable-state, DB-before-side-effect, lifecycle, and reconciliation rules
are owned by `state-reconciliation` plus the canonical invariants.

For SSH specifically:

- only definite pre-launch failure may be treated as a definite launch failure;
  timeout/drop/unknown launch outcomes remain ambiguous against the same
  target/idempotency identity;
- terminal status comes from reviewed durable sentinel evidence, not log text or
  tmux liveness;
- unreachable transport means unknown/skip, never fabricated execution failure;
- stop uses the reviewed approved-stop path;
- preserve the currently reviewed host-key policy; changing host-identity
  validation requires the applicable named decision.

## Boundary with Development Plane

- Never expose arbitrary remote-command parameters/endpoints/tools.
- Development Agent code never imports/receives unrestricted SSH/local subprocess execution.
- New validation/execution mechanisms require named architecture semantics; do not grow SSH into a private agent shell.
- Free-text onboarding/workspace values must be validated/quoted before any command boundary.

## Validation

Add focused pure-builder and FakeSSH tests for changed command/execution paths. Never contact real hosts, keys, services, or runtime data.
