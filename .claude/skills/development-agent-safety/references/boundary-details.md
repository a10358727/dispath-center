# Development Agent boundary — operational details

Detail companion to `SKILL.md`. The domain model (DevelopmentAgent /
AgentProvider / AgentSession), the selection principles, and the full
allowed/forbidden lists live in
`../../dispatcher-domain/references/development-platform.md` §4/§5.

## Provider neutrality

- The safety boundary is defined **per plane, not per provider**. No provider
  gets a wider tool set, a private execution channel, or a relaxed approval
  path because of what it is. Switching or selecting a provider must never be
  a privilege escalation.
- Core domain objects (Project, Workspace, ProjectVersion, ExecutionPlan,
  Approval) must not depend on a specific provider. Provider specifics live
  only inside the allowlisted adapter registry (`app/coding_agents.py`).
- Do not write provider CLI details (Codex CLI flags, Claude Code invocation,
  auth modes) into domain models, schemas, or shared contracts — they belong
  in the provider adapter and its configuration.
- Codex remains the current implemented provider (`codex-exec-v1`, bounded
  `codex-app-server-v1`). Referring to that implementation is fine; treating
  "Codex" as the name of the abstraction is not.

## Agent selection

Two modes are accepted product direction (mostly not built yet):

- **Manual** — the user explicitly picks a provider (e.g. Codex or Claude
  Code) from the configured allowlist.
- **Auto** — the system picks deterministically from: configured/installed
  state, current availability, supported capabilities, project requirements,
  policy, and session requirements.

Any Auto selection implementation must be:

- deterministic and explainable — the same inputs yield the same provider,
  and the reason is derivable from recorded data;
- recorded — the selected provider id is persisted with the session/run;
- authorization-preserving — selection never grants capabilities beyond the
  shared Development Plane boundary, never bypasses approval, ExecutionPlan,
  Dataset permission, or the SSH boundary;
- fallback-safe — no silent fallback to a different provider, and never to
  one with broader effective access; a failed or drifted provider fails
  closed and reports.

A request selects a **provider id from the reviewed allowlist**, never an
executable or a command line.

## Workspace rules

- Work happens in a dispatch-created git worktree on its own branch, based on
  an exact commit. The base is immutable for the duration of the turn.
- Instruction text is user free text: it is written to an instruction file and
  fed via stdin/file, never interpolated into a shell command (`INV-SSH-2`).
  The task script is assembled deterministically by pure `build_*` functions —
  the model never writes shell.
- Dependency installs stay inside the workspace (its own venv), never system
  packages or system Python.
- A dirty worktree never produces a promotable result. Convergence is verified
  before a bundle is produced.
- Cleanup is a separate, explicitly approved operation. Promotion does not
  delete a worktree.

## Approval rules

- `coding_task`, `apply_patch`, `engineering_task_retry`,
  `engineering_task_discard` and `engineering_task_promote` are all
  approval-gated and **never** auto-approved — the allowlist stays exactly
  `enqueue|stop` (`INV-APPROVAL-4`), and policy-scoped auto decisions are
  limited to `auto_placement` (`INV-APPROVAL-4b`). Adding a provider never
  adds an auto-approval path.
- `engineering_task_pr` and `engineering_task_finalize` are deliberately
  absent. Creating them requires a named user ruling; do not add them because
  a plan document mentions them.
- Promotion (`engineering_task_promote`) is human-only by design
  (DG-CODE-PROMOTE-v1 P-1), regardless of which provider produced the change.
  Approve time re-verifies bundle bytes, runs a real `git bundle verify` in
  staging, creates a non-runnable ProjectVersion, and publishes only to the
  local hub — never GitHub (P-4). A repeat promote of the same commit is a
  no-op returning the existing version (P-2). A `project_versions` row without
  a promotion approval is `legacy_observed` and cannot back a reproducible
  run (P-3).

## Execution rules

- An agent turn is dispatched as a normal approved Job through the existing
  SSH/tmux/sentinel infrastructure; no provider gets a private execution path.
  Terminal status still comes only from the sentinel `exit_code`
  (`INV-SSH-6`), and stopping it still requires an approved stop (`INV-SSH-9`).
- Runner selection is server-side. The requester does not pick an arbitrary
  machine; the coding target is the configured runner (today
  `CODEX_RUNNER_SERVER` — a deployment fact, not part of the abstraction).
- Adapters are an allowlisted, version-pinned registry. Version or
  message-shape drift fails closed; a turn that may already have caused a
  side effect never silently falls back to another adapter.
- `CONTROLLED_CODING_RUNNER_V1` and `ENGINEERING_TASK_BACKEND_V1` are default
  off. Enabling either in a real environment is a separate signed decision.
