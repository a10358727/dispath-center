# Development Agent boundary — operational details

Detail companion to `SKILL.md`. The domain model (DevelopmentAgent /
AgentProvider / AgentSession), the selection principles, and the full
allowed/forbidden lists live in
`docs/PLATFORM_CHARTER.md` §4.1/§4.3.

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
- The registry and its tests are the implementation truth for which
  providers/adapters currently exist. Referring to a specific implemented
  adapter is fine; treating "Codex" as the name of the abstraction is not.

## Agent selection

Per DG-PRODUCT-PLAN-CORRECTIONS v1, Manual selection plus a per-Project
default provider is the product requirement; Auto is an optional extension.
Which selection capabilities exist now is answered by code + tests and the
named rulings in `docs/DECISIONS.md`, not by this file.

- **Manual** — the user explicitly picks a provider id from the approved
  registry, limited to providers currently enabled.
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

- Every Development Plane approval kind (the `coding_task`/`apply_patch`/
  `engineering_task_*`/`agent_session_*` family — the current set is
  `VALID_APPROVAL_KINDS` in `app/db.py`) is approval-gated and **never**
  auto-approved — the allowlist stays exactly `enqueue|stop`
  (`INV-APPROVAL-4`), and policy-scoped auto decisions are limited to
  `auto_placement` (`INV-APPROVAL-4b`). Adding a provider never adds an
  auto-approval path.
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

## Validation and execution rules

Keep two layers separate:

**Development validation (abstract contract, all providers).** Validation
(test/lint/typecheck/build) runs inside the isolated workspace through a
dispatch-controlled path that is bounded, auditable, deterministic in how it
is assembled, and never a general-purpose shell. This contract does **not**
require every provider to use the same mechanism — but every mechanism must
trace to a named ruling in `docs/DECISIONS.md`, introducing a new one is an
architecture decision needing a new named ruling, and no mechanism may weaken
approval, audit, isolation, or the SSH boundary. Verify which mechanisms
exist, and which one a provider uses, from code + tests and those rulings.

**Mechanism rules (implementation facts, not abstract requirements).** For
any Job-backed or tmux/sentinel-backed mechanism: terminal status comes only
from the sentinel `exit_code` (`INV-SSH-6`), stopping requires an approved
stop (`INV-SSH-9`), runner selection is server-side (the requester never
picks an arbitrary machine; the configured runner is a deployment fact, not
part of the abstraction), and no provider using it gets a private channel or
relaxed monitoring.

**Compute execution (all providers, always).** Training/GPU/worker workloads
never launch from a workspace: they re-enter the Compute Plane via promoted
ProjectVersion → ExecutionPlan → approval → dispatch.

- Adapters are an allowlisted, version-pinned registry. Version or
  message-shape drift fails closed; a turn that may already have caused a
  side effect never silently falls back to another adapter.
- Agent-related rollout flags gate these paths; check current defaults in
  `app/config.py`/`app/settings/features.py` and rollout status in
  `docs/CAPABILITY_LEDGER.md`. Enabling one in a real environment is a
  separate signed decision, never a side effect of a merge.
