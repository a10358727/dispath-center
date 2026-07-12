---
name: ssh-dispatch-safety
description: Protect remote-command safety. Use for SSH/SFTP/rsync/tmux, shell interpolation, build_* commands/scripts, dispatch/stop, timeouts/retries, or remote metric parsing.
---

# SSH Dispatch Safety

Read the relevant `INV-SSH-*` and `INV-STATE-2` sections in `../dispatcher-domain/references/invariants.md`.

Required checks:

- Build commands in testable pure functions; validate or `shlex.quote` every interpolated value.
- Send user command text only through SFTP, never shell interpolation.
- Give every SSH call a timeout; run long jobs detached in tmux.
- Preserve DB-before-side-effect ordering, sentinel-based completion, and unreachable-means-no-state-change.
- Stop jobs only through approved stop flow; do not alter the recorded host-key policy implicitly.
- Add exact command/FakeSSH tests for changed builders or execution paths.

Never contact real hosts, read private keys, start services, or mutate runtime DB/audit/server files.
