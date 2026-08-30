---
name: ssh-dispatch-safety
description: Protect the Compute Plane SSH backend. Use for SSH/SFTP/rsync/tmux, remote command builders, dispatch/stop, timeouts/retries, remote probes/metrics, or Development Plane crossings into worker execution.
---

# SSH Dispatch Safety

SSH is a closed-shape execution backend, never a general-purpose remote shell.

## Read only what applies

- `references/ssh-contract.md` — command, recovery, and Development/Compute boundary checklist.
- Relevant `INV-SSH-*` and `INV-STATE-2` in `docs/PLATFORM_CHARTER.md` §6.
- `docs/DECISIONS.md` when adding/changing an execution or validation mechanism.

## Hard boundary

- Build remote commands through reviewed pure builders; user/instruction text is data, never shell interpolation.
- Preserve timeout, idempotency, DB-before-side-effect, sentinel, unknown-not-failed, and approved-stop semantics.
- Never expose arbitrary remote command execution or give Development Agents direct SSH/subprocess access.
- A new execution mechanism is an architecture decision, not an SSH exception.

## Validation

Run focused builder/FakeSSH/execution-path tests only. Never contact real hosts, keys, services, or runtime data.
