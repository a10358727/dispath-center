---
name: ssh-dispatch-safety
description: Use for SSH/SFTP/rsync/tmux, remote command builders, worker dispatch/stop, remote probes/metrics, timeouts/retries, sentinel evidence, or any Development-to-Compute SSH crossing.
---

# SSH Dispatch Safety

SSH is a closed-shape execution backend, never a general-purpose remote shell.

## Rules

- Build remote commands through reviewed pure/testable builders.
- User/instruction text is data, never unreviewed shell interpolation.
- Read-only probes use a closed command set and secret filtering.
- Every SSH operation has reviewed timeout/idempotency behavior.
- Ambiguous launch outcomes keep the same execution/target identity.
- Terminal execution truth comes from reviewed durable sentinel evidence, not
  log text or tmux liveness.
- Unreachable transport is unknown/skip, never fabricated execution failure.
- Stop only through the reviewed stop path.
- Never expose arbitrary remote-command tools to a Development Agent.
- Preserve the currently reviewed host-key policy; changing host identity
  validation requires the applicable named decision.

`state-reconciliation` owns generic DB/lifecycle/recovery semantics.

## Validation

Run focused pure-builder/FakeSSH/execution-path tests only. Never contact real
hosts, keys, services, or runtime data.
