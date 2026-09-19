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

## Approval consequences for Development Agents

Generic approval/auth/auto-approval mechanics are owned by
`approval-boundary`, the canonical invariants, and named Decisions. Do not
duplicate their allowlists or lifecycle rules here.

Development-Agent-specific consequences are:

- a Development Agent may create reviewed proposals/requests but never decide
  its own approval;
- adding or switching a provider never creates a wider approval path;
- ProjectVersion promotion remains human-governed and must follow the current
  named promotion ruling and implementation;
- a ProjectVersion that does not satisfy the current promotion/provenance
  requirements must not be treated as reproducible Compute input.

If approval mechanics themselves change, load `approval-boundary` and verify the
current implementation/tests instead of editing an approval rule in this file.

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

**Mechanism rules (implementation facts, not abstract requirements).** Do not
redefine SSH launch/recovery semantics here. When a validation/execution
mechanism uses SSH/tmux/sentinel, `ssh-dispatch-safety` owns transport and
sentinel behavior and `state-reconciliation` owns generic durable/recovery
semantics. The Development-Agent requirement is only that no provider gains a
private channel, arbitrary target selection, or relaxed monitoring.

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
