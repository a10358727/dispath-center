# SSH execution contract

Canonical semantics are in the relevant `INV-SSH-*` / `INV-STATE-*` sections of `../../dispatcher-domain/references/invariants.md` and named decisions.

## Command construction

- Keep remote command builders pure/testable; validate or quote every interpolated value.
- User/instruction text is data transferred through SFTP, never shell fragments.
- Read-only probes use a closed enumerated command set with secret filtering.
- Every SSH call has a timeout; long jobs detach through the reviewed tmux/sentinel mechanism.

## State and recovery

- Persist state before remote side effects.
- Only definite pre-launch failure may revert launch state; timeout/drop/unknown outcomes are ambiguous and preserve the same target/idempotency identity.
- Terminal status comes from durable sentinel evidence, not log text or tmux liveness.
- Unreachable means unknown/skip, never failed.
- Stop only through the approved stop flow; preserve reviewed host-key policy.

## Boundary with Development Plane

- Never expose arbitrary remote-command parameters/endpoints/tools.
- Development Agent code never imports/receives unrestricted SSH/local subprocess execution.
- New validation/execution mechanisms require named architecture semantics; do not grow SSH into a private agent shell.
- Free-text onboarding/workspace values must be validated/quoted before any command boundary.

## Validation

Add focused pure-builder and FakeSSH tests for changed command/execution paths. Never contact real hosts, keys, services, or runtime data.
