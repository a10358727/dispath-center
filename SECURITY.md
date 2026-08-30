# Security Policy

Dispatch Center is an Agent-native Engineering Platform that governs remote
execution on behalf of AI agents and people. Treat authorization, approval
payloads, node credentials, SSH command construction, agent workspace
isolation, audit evidence, and state reconciliation as security boundaries.

## Supported versions

The project has not published its first stable release. Security fixes are
provided on the current default branch and on explicitly documented supported
release branches once those releases exist. Historical phase branches are not
supported releases.

## Reporting a vulnerability

Do not open a public issue containing credentials, private topology, exploit
details, or production data. Report the issue privately to the repository
owner through GitHub's private vulnerability reporting channel. Include:

- affected revision or release;
- impacted component and trust boundary;
- minimal reproduction using synthetic data;
- expected and observed behavior;
- whether credentials, remote execution, approval immutability, or cross-project
  access may be affected.

Do not test against production systems, workers, credentials, or user data
without explicit authorization.

## Security invariants

The canonical security and correctness requirements live in
`docs/PLATFORM_CHARTER.md`. In particular:

- material mutations use the approval workflow;
- approved payloads remain immutable and are revalidated at approval time;
- user command bytes reach SSH workers through SFTP, never shell interpolation;
- database state changes precede external side effects;
- unreachable or ambiguous execution state is not fabricated as failure;
- LLM and MCP surfaces can query or request approval, but cannot approve,
  execute arbitrary shell, or connect directly to SSH;
- Node assignment stays disabled until isolation and real canary gates pass;
- tests never contact real infrastructure.

A proposed fix that changes one of these invariants must stop for an explicit
architecture decision rather than silently weakening the boundary.

## Secrets

Never commit `.env`, service or node tokens, OIDC client secrets, SSH private
keys, production database copies, audit exports, or real server inventories.
Logs and error messages must redact credentials. Secret rotation and revocation
must remain possible per identity or node.
