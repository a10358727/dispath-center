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
