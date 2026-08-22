---
name: development-agent-safety
description: Protect the Development Agent boundary for every provider (Codex, Claude Code, future coding agents). Use for coding/development agents, agent sessions, agent selection, coding_task, engineering tasks, coding runs, isolated workspace/worktree, code sessions and code editing, project-local validation (test/lint/typecheck/build), diffs, apply_patch, or ProjectVersion promotion.
---

# Development Agent Safety (Development Plane)

**A Development Agent is a Development Plane collaborator, never an
unrestricted SSH or root agent.** This holds identically for every provider —
Codex (the only implemented provider), Claude Code, and any future coding
agent. Selecting or switching a provider (manually or via Auto selection) is
never a privilege escalation.

## Required reads

- `../dispatcher-domain/references/development-platform.md` §4/§5 — plane
  model, DevelopmentAgent/AgentProvider/AgentSession, selection principles.
- `references/boundary-details.md` — provider neutrality, agent selection,
  workspace, approval, and execution rules in full.
- Relevant `INV-LLM-*`, `INV-APPROVAL-1/2/3/4`, `INV-SSH-2/3/6/9` sections in
  `../dispatcher-domain/references/invariants.md`.

## Hard rules

Two kinds of "running things" are never the same thing:

- **Development validation** — controlled test/lint/typecheck/build inside the
  dispatch-created isolated workspace, through a dispatch-controlled,
  bounded, auditable validation path (never a general-purpose shell).
- **Compute execution** — training/GPU/worker workloads. These always
  re-enter the Compute Plane via promoted ProjectVersion → ExecutionPlan →
  approval → dispatch, regardless of provider.

A Development Agent may only: read/edit files inside its dispatch-created
isolated workspace/worktree; run development validation as defined above;
produce a reviewable diff; propose a next action as a pending approval.

It must never:

- approve or reject any request, including its own (`INV-LLM-1/2`);
- get unrestricted SSH or any direct execution/subprocess handle (`INV-LLM-3`);
- receive a `shell`/`exec`/`run_command` tool under any justification;
- read or mutate runtime DB, audit log, or server config directly;
- bypass authorization, Dataset permission, ExecutionPlan, or promotion rules;
- deploy or promote anything itself — those are separate human-approved actions;
- hold a credential or get unrestricted secret access (secrets never appear in
  its prompt, diff, logs, artifacts, or audit records);
- choose its own workspace path, write outside its worktree, modify a live
  project instance, or push to an external origin;
- act on an analysis recommendation without a new approved request.

Promotion is human-only (DG-CODE-PROMOTE-v1 P-1) and the coding approval
kinds are never auto-approved (allowlist stays exactly `enqueue|stop`).

**Do not invent:** no agent conversation domain, AgentSession state machine,
streaming session lifecycle, provider-selection engine, or Claude Code adapter
exists. Do not add statuses, tables, or registry entries for them ahead of a
decision — stop and report instead.

## Validation

Normally: `pytest tests/test_coding_task.py tests/test_coding_agents.py
tests/test_engineering_tasks.py tests/test_engineering_task_job_safety.py
tests/test_codex_app_server.py tests/test_agent_tools.py -q`

Cover: approval required; dangerous/invalid instruction rejected at request
time; command-string assertions for changed builders; dirty-worktree refusal;
unknown-provider rejection; forbidden-module/forbidden-tool-name pins intact
(never weaken them, `INV-TEST-2`).

Never contact a real agent provider, runner, worker, or credentials; never
mutate runtime `jobqueue.db`/`audit.jsonl`/`servers.yaml`.
