# Codex Skills

Dispatch Center's Codex-specific repository skills live in:

```text
.agents/skills/
```

Codex discovers repository skills automatically. No local installation step is
required for these project-specific skills.

## Repository skills

- `dispatcher-domain` — cross-domain routing
- `project-onboarding` — Project discovery/import/instances
- `development-agent-safety` — product Development Agent boundary
- `approval-boundary` — approval/auth/mutation mechanics
- `ssh-dispatch-safety` — SSH/tmux/sentinel execution mechanics
- `state-reconciliation` — durable state/lifecycle/recovery
- `frontend-architecture` — Studio UI/UX projection
- `release-gate` — explicit exact-commit release verification
- `updating-pilot-site` — explicit pilot deployment/rollback only

Use the most specific matching skill. A domain skill owns **what** is changing;
mechanism/boundary skills are added only when the task crosses that boundary.

## Verify Codex discovery

Start/restart Codex from the repository, then use:

```text
/skills
```

or type `$` in the Codex prompt to browse/reference available skills.

If newly committed skills do not appear, restart the Codex session.

## Optional external skills

These are useful but are **not required** by Dispatch Center.

### React best practices

Useful for Studio React implementation/review:

```bash
npx skills add https://github.com/vercel-labs/agent-skills --skill vercel-react-best-practices
```

Review third-party skill contents before adopting/updating them.

### Webapp testing

Useful for local browser/E2E verification with Playwright:

```bash
npx skills add https://github.com/anthropics/skills --skill webapp-testing
```

Do not let an external testing skill override Dispatch Center's rules against
production endpoints, real credentials, or runtime data.

## Recommended model routing

Dispatch Center uses model tiers by responsibility rather than running every
subagent at the highest tier:

```text
Main integration / cross-domain reasoning
→ gpt-5.6-sol · xhigh

Read-only repository exploration / architecture mapping
→ gpt-5.6-terra · high

Bounded implementation / small workers
→ gpt-5.6-luna · high/max
```

The project-level defaults live in `.codex/config.toml`. Specialized agents in
`.codex/agents/*.toml` override those defaults when appropriate.

Keep `max_concurrent_threads_per_session = 2` unless there is concrete evidence
that more parallelism improves a specific work packet; this repository has
shared API/state/UI boundaries where excessive parallel editing can create
conflicts.

## Codex built-in installer

Codex also supports the built-in skill installer. In Codex invoke:

```text
$skill-installer
```

Use it for curated/local experimentation. Repository-specific Dispatch Center
skills should remain committed under `.agents/skills/` so every contributor
gets the same project rules.

## Governance

External skills are advisory implementation aids. They never override:

1. `AGENTS.md`
2. Charter + Decisions
3. current code/tests
4. Capability Ledger
5. V0.1 Architecture / UX / Implementation Plan

Do not install deployment, Kubernetes, provider, or infrastructure skills merely
because they exist publicly; V0.1 explicitly keeps those capabilities bounded.
