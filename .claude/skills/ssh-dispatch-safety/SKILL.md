---
name: ssh-dispatch-safety
description: Protect the Compute Plane SSH execution backend. Use for SSH/SFTP/rsync/tmux, shell interpolation, build_* commands/scripts, dispatch/stop, timeouts/retries, remote metric parsing, or to check that Development Plane work does not improperly cross the SSH boundary.
---

# SSH Dispatch Safety (Compute Plane)

This skill owns the **Compute Plane SSH backend** — a closed-shape execution
capability, not a general-purpose remote shell. Its second job is boundary
review: checking that Development Plane work consumes this layer without
extending it.

Read the relevant `INV-SSH-*` and `INV-STATE-2` sections in
`../dispatcher-domain/references/invariants.md`.

## Required checks

- Build commands in testable pure functions; validate or `shlex.quote` every
  interpolated value. No inline command assembly at the call site.
- Send user command text only through SFTP, never shell interpolation.
- Give every SSH call a timeout; run long jobs detached in tmux and return
  immediately.
- Read-only probes (inventory, activity, test-ssh) stay a closed, enumerated
  command set with secret filtering done in the Python layer.
- Preserve DB-before-side-effect ordering. Only a *definite* pre-launch failure
  may revert `running` → `queued`; timeouts, dropped connections and unknown
  exceptions are **ambiguous**, keep the job running, and are retried against
  the same target with the same idempotency key.
- Terminal status comes only from the sentinel `exit_code`; never infer it from
  log text or tmux liveness. Unreachable means skip this round, never failed.
- Stop only through the approved stop flow. Do not change the recorded
  host-key policy implicitly.
- The SSH backend never assumes a Node Agent exists; it stays the permanent
  compatibility/emergency channel for every worker.
- Add exact command/FakeSSH tests for changed builders or execution paths.

## Development Plane must not use dispatch as a shell

Onboarding, development agents (Codex, Claude Code, or any provider),
workspace and promotion features are **consumers** of this
layer, never extenders of it:

- No "run this arbitrary command on this server" endpoint, tool, or parameter —
  regardless of who or what supplies the string. An agent-supplied command
  string is exactly the input this boundary exists to prevent.
- No new remote-execution path that skips `request_*` → `approve()`.
- No Development Plane caller may import `sshpool`/`localrun`/`subprocess`
  directly; agent-facing code receives an injected `ssh_run` callable limited to
  the closed read-only command set (`INV-LLM-3`, `INV-SSH-4`).
- The current coding-turn implementation dispatches as an ordinary approved
  Job through the existing sentinel protocol; anything on this path gets no
  private channel, no relaxed timeout, no unmonitored session, and no
  exemption from approved-stop. A different validation mechanism for a future
  provider is an architecture decision, not something this layer grows to
  accommodate.
- Instruction and diff text are data written by SFTP, never command fragments.
- Free-text values from onboarding (paths, project names, branches) are
  validated against an explicit character set or quoted before they reach a
  command string.

If a Development Plane requirement genuinely cannot be met inside these rules,
stop and report it as a decision, not as an exception in code.

Never contact real hosts, read private keys, start services, or mutate runtime
DB/audit/server files.
